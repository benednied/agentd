#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef REAL_BWRAP
#define REAL_BWRAP "/usr/libexec/agentd/bwrap.real"
#endif
#ifndef AUDIT_DIRECTORY
#define AUDIT_DIRECTORY "/run/agentd"
#endif
#ifndef AUDIT_FILENAME
#define AUDIT_FILENAME "bwrap-compat.audit"
#endif
#ifndef AUDIT_TOKEN_ENV
#define AUDIT_TOKEN_ENV "AGENTD_BWRAP_AUDIT_TOKEN"
#endif
#ifndef REJECTED_EXIT_STATUS
#define REJECTED_EXIT_STATUS 64
#endif

extern char **environ;

struct inspection {
    int rewrite_index;
    bool device_seen;
    bool unshare_net_seen;
};

static _Noreturn void fail(const char *message) {
    (void)dprintf(
        STDERR_FILENO,
        "agentd bwrap compatibility shim: %s\n",
        message
    );
    _exit(REJECTED_EXIT_STATUS);
}

static _Noreturn void fail_errno(const char *operation) {
    const int saved_errno = errno;
    (void)dprintf(
        STDERR_FILENO,
        "agentd bwrap compatibility shim: %s: %s\n",
        operation,
        strerror(saved_errno)
    );
    _exit(REJECTED_EXIT_STATUS);
}

static bool matches(
    const char *argument,
    const char *const *options,
    size_t option_count
) {
    for (size_t index = 0; index < option_count; index++) {
        if (strcmp(argument, options[index]) == 0) {
            return true;
        }
    }
    return false;
}

static bool is_no_operand_option(const char *argument) {
    static const char *const options[] = {
        "--help",
        "--version",
        "--unshare-all",
        "--unshare-user",
        "--unshare-user-try",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup",
        "--unshare-cgroup-try",
        "--disable-userns",
        "--assert-userns-disabled",
        "--clearenv",
        "--new-session",
        "--die-with-parent",
        "--as-pid-1",
    };
    return matches(argument, options, sizeof(options) / sizeof(options[0]));
}

static bool is_one_operand_option(const char *argument) {
    static const char *const options[] = {
        "--userns",
        "--userns2",
        "--pidns",
        "--uid",
        "--gid",
        "--hostname",
        "--chdir",
        "--unsetenv",
        "--lock-file",
        "--sync-fd",
        "--remount-ro",
        "--exec-label",
        "--file-label",
        "--proc",
        "--tmpfs",
        "--mqueue",
        "--dir",
        "--seccomp",
        "--add-seccomp-fd",
        "--block-fd",
        "--userns-block-fd",
        "--info-fd",
        "--json-status-fd",
        "--cap-add",
        "--cap-drop",
        "--perms",
        "--size",
    };
    return matches(argument, options, sizeof(options) / sizeof(options[0]));
}

static bool is_two_operand_option(const char *argument) {
    static const char *const options[] = {
        "--setenv",
        "--bind",
        "--bind-try",
        "--ro-bind",
        "--ro-bind-try",
        "--bind-fd",
        "--ro-bind-fd",
        "--file",
        "--bind-data",
        "--ro-bind-data",
        "--symlink",
        "--chmod",
    };
    return matches(argument, options, sizeof(options) / sizeof(options[0]));
}

static void require_operands(int argc, int index, int count) {
    if (count < 0 || index > argc - count - 1) {
        fail("truncated Bubblewrap option");
    }
}

static struct inspection inspect_arguments(int argc, char *const argv[]) {
    struct inspection result = {
        .rewrite_index = -1,
        .device_seen = false,
        .unshare_net_seen = false,
    };

    for (int index = 1; index < argc;) {
        const char *argument = argv[index];
        if (strcmp(argument, "--") == 0 || argument[0] != '-') {
            break;
        }
        if (argument[0] != '-' || argument[1] != '-') {
            fail("short or malformed Bubblewrap option rejected");
        }
        if (strchr(argument, '=') != NULL) {
            fail("equals-style Bubblewrap option rejected");
        }
        if (strcmp(argument, "--args") == 0) {
            fail("--args could conceal an unreviewed device or network option");
        }
        if (strcmp(argument, "--share-net") == 0) {
            fail("--share-net is forbidden");
        }
        if (
            strcmp(argument, "--unshare-net") == 0 ||
            strcmp(argument, "--unshare-all") == 0
        ) {
            result.unshare_net_seen = true;
            index++;
            continue;
        }
        if (strcmp(argument, "--dev") == 0) {
            require_operands(argc, index, 1);
            if (result.device_seen || strcmp(argv[index + 1], "/dev") != 0) {
                fail("only one exact --dev /dev declaration is allowed");
            }
            result.device_seen = true;
            result.rewrite_index = index;
            index += 2;
            continue;
        }
        if (strcmp(argument, "--dev-bind") == 0) {
            require_operands(argc, index, 2);
            if (
                result.device_seen ||
                strcmp(argv[index + 1], "/dev") != 0 ||
                strcmp(argv[index + 2], "/dev") != 0
            ) {
                fail("only one exact --dev-bind /dev /dev declaration is allowed");
            }
            result.device_seen = true;
            index += 3;
            continue;
        }
        if (strcmp(argument, "--dev-bind-try") == 0) {
            fail("--dev-bind-try is forbidden");
        }
        if (is_no_operand_option(argument)) {
            index++;
            continue;
        }
        if (is_one_operand_option(argument)) {
            require_operands(argc, index, 1);
            index += 2;
            continue;
        }
        if (is_two_operand_option(argument)) {
            require_operands(argc, index, 2);
            index += 3;
            continue;
        }
        fail("unknown Bubblewrap option rejected");
    }
    return result;
}

static bool valid_audit_token(const char *token) {
    if (token == NULL || strlen(token) != 32) {
        return false;
    }
    for (size_t index = 0; index < 32; index++) {
        const char value = token[index];
        if (!((value >= '0' && value <= '9') || (value >= 'a' && value <= 'f'))) {
            return false;
        }
    }
    return true;
}

static void write_all(int descriptor, const char *buffer, size_t length) {
    size_t offset = 0;
    while (offset < length) {
        const ssize_t written = write(descriptor, buffer + offset, length - offset);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            fail_errno("write audit record");
        }
        if (written == 0) {
            fail("short write to audit record");
        }
        offset += (size_t)written;
    }
}

static void write_audit_record(
    const char *token,
    const struct inspection *inspection
) {
    if (token == NULL) {
        return;
    }
    if (!valid_audit_token(token)) {
        fail("invalid audit token");
    }

    const int directory = open(
        AUDIT_DIRECTORY,
        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
    );
    if (directory < 0) {
        fail_errno("open audit directory");
    }
    struct stat metadata;
    if (fstat(directory, &metadata) != 0) {
        fail_errno("inspect audit directory");
    }
    if (
        !S_ISDIR(metadata.st_mode) || metadata.st_uid != geteuid() ||
        metadata.st_gid != getegid() || (metadata.st_mode & 0777) != 0700
    ) {
        fail("audit directory is not a private service-user directory");
    }

    const int audit = openat(
        directory,
        AUDIT_FILENAME,
        O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_CLOEXEC,
        0600
    );
    if (audit < 0) {
        fail_errno("open audit record");
    }
    if (fstat(audit, &metadata) != 0) {
        fail_errno("inspect audit record");
    }
    if (
        !S_ISREG(metadata.st_mode) || metadata.st_uid != geteuid() ||
        metadata.st_gid != getegid() || (metadata.st_mode & 0777) != 0600 ||
        metadata.st_nlink != 1
    ) {
        fail("audit record is not a private single-link service-user file");
    }

    char record[192];
    const int length = snprintf(
        record,
        sizeof(record),
        "v1 token=%s pid=%ld rewrite_dev=%d unshare_net=%d\n",
        token,
        (long)getpid(),
        inspection->rewrite_index >= 0 ? 1 : 0,
        inspection->unshare_net_seen ? 1 : 0
    );
    if (length < 0 || (size_t)length >= sizeof(record)) {
        fail("audit record overflow");
    }
    write_all(audit, record, (size_t)length);
    if (fsync(audit) != 0) {
        fail_errno("flush audit record");
    }
    if (close(audit) != 0 || close(directory) != 0) {
        fail_errno("close audit record");
    }
}

static int open_verified_real_bwrap(void) {
    const int descriptor = open(REAL_BWRAP, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        fail_errno("open private Bubblewrap binary");
    }
    struct stat metadata;
    if (fstat(descriptor, &metadata) != 0) {
        fail_errno("inspect private Bubblewrap binary");
    }
    if (
        !S_ISREG(metadata.st_mode) || metadata.st_uid != 0 || metadata.st_gid != 0 ||
        (metadata.st_mode & 0022) != 0 || (metadata.st_mode & 0111) == 0
    ) {
        fail("private Bubblewrap binary is not root-owned and immutable");
    }
    return descriptor;
}

int main(int argc, char *argv[]) {
    const struct inspection inspection = inspect_arguments(argc, argv);
    const size_t extra = inspection.rewrite_index >= 0 ? 1U : 0U;
    if ((size_t)argc > SIZE_MAX - extra - 1U) {
        fail("argument vector overflow");
    }
    char **rewritten = calloc((size_t)argc + extra + 1U, sizeof(*rewritten));
    if (rewritten == NULL) {
        fail_errno("allocate rewritten argument vector");
    }

    size_t output = 0;
    rewritten[output++] = (char *)REAL_BWRAP;
    for (int index = 1; index < argc; index++) {
        if (index == inspection.rewrite_index) {
            rewritten[output++] = (char *)"--dev-bind";
            rewritten[output++] = (char *)"/dev";
            rewritten[output++] = (char *)"/dev";
            index++;
            continue;
        }
        rewritten[output++] = argv[index];
    }
    rewritten[output] = NULL;

    const char *audit_token = getenv(AUDIT_TOKEN_ENV);
    write_audit_record(audit_token, &inspection);
    if (audit_token != NULL && unsetenv(AUDIT_TOKEN_ENV) != 0) {
        fail_errno("clear audit token");
    }

    const int real_bwrap = open_verified_real_bwrap();
    fexecve(real_bwrap, rewritten, environ);
    fail_errno("execute private Bubblewrap binary");
}
