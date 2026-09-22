from dataclasses import dataclass

from agentd.intake.backlog import GitHubBacklogSource, ReadBound
from agentd.intake.models import IntakePolicy


@dataclass
class FakeTransport:
    pages: list[list[dict]]
    edges: dict[int, list[dict]]
    direct: dict[int, dict] | None = None
    children: dict[int, list[dict]] | None = None

    def repository(self, repository):
        assert repository == "acme/app"
        return {"id": 7}

    def issues(self, repository, *, page, per_page):
        return self.pages[page - 1] if page <= len(self.pages) else []

    def blockers(self, repository, number):
        return self.edges.get(number, [])

    def issue(self, repository, number):
        for item in self.pages[0]:
            if item["number"] == number:
                return item
        if self.direct and number in self.direct:
            return self.direct[number]
        raise LookupError(number)

    def sub_issues(self, repository, number, *, page, per_page):
        values = (self.children or {}).get(number, [])
        return values[(page - 1) * per_page : page * per_page]


def issue(number, node, title=None):
    return {
        "id": number * 100,
        "number": number,
        "node_id": node,
        "title": title or f"Issue {number}",
        "body": "work",
        "updated_at": "2026-09-20T12:00:00Z",
        "state": "open",
        "labels": [{"name": "agentd:approved"}],
    }


def test_discovery_is_stable_and_topological():
    transport = FakeTransport(
        [[issue(2, "I2"), issue(1, "I1")]],
        {2: [{"number": 1, "node_id": "I1"}]},
    )
    source = GitHubBacklogSource(transport, per_page=100)
    first = source.discover("ACME/APP", bound=ReadBound.EXACT)
    second = source.discover("acme/app", bound=ReadBound.EXACT)
    assert first.complete and first.authorizable
    assert first.revision == second.revision
    assert first.order == ("github:7:I1", "github:7:I2")
    assert first.blockers["github:7:I2"] == ("github:7:I1",)
    assert first.to_dict() == second.to_dict()


def test_bounded_read_is_inspection_only_when_page_limit_is_hit():
    transport = FakeTransport([[issue(1, "I1")], [issue(2, "I2")]], {})
    snapshot = GitHubBacklogSource(transport, per_page=1, max_pages=1).discover(
        "acme/app", bound=ReadBound.BOUNDED
    )
    assert not snapshot.complete
    assert not snapshot.authorizable
    assert not snapshot.selects("github:7:I1", IntakePolicy("acme/app", 7))


def test_exact_read_fails_closed_on_incomplete_pagination():
    transport = FakeTransport([[issue(1, "I1")], [issue(2, "I2")]], {})
    snapshot = GitHubBacklogSource(transport, per_page=1, max_pages=1).discover(
        "acme/app", bound=ReadBound.EXACT
    )
    assert not snapshot.complete
    assert "pagination" in (snapshot.error or "")


def test_missing_dependency_fails_closed_and_round_trips():
    transport = FakeTransport(
        [[issue(2, "I2")]],
        {2: [{"number": 1, "node_id": "I1"}]},
    )
    source = GitHubBacklogSource(transport)
    snapshot = source.discover("acme/app", bound=ReadBound.EXACT)
    assert snapshot.missing_dependencies == ("github:7:I1",)
    assert not snapshot.authorizable
    assert type(snapshot).from_dict(snapshot.to_dict()) == snapshot


def test_dependency_payload_without_immutable_identity_fails_closed():
    transport = FakeTransport([[issue(1, "I1")]], {1: [{"number": 2}]})
    snapshot = GitHubBacklogSource(transport).discover(
        "acme/app", bound=ReadBound.EXACT
    )
    assert not snapshot.authorizable
    assert "dependencies" in (snapshot.error or "")


def test_cycle_is_deterministic_but_not_authorized():
    transport = FakeTransport(
        [[issue(1, "I1"), issue(2, "I2")]],
        {1: [{"number": 2, "node_id": "I2"}], 2: [{"number": 1, "node_id": "I1"}]},
    )
    snapshot = GitHubBacklogSource(transport).discover(
        "acme/app", bound=ReadBound.EXACT
    )
    assert snapshot.order == ("github:7:I1", "github:7:I2")
    assert not snapshot.authorizable
    assert "cycle" in (snapshot.error or "")


def test_epic_reads_subissues_and_keeps_dependencies_out_of_members():
    epic = issue(77, "I77", "Epic")
    child = issue(78, "I78", "Child")
    dependency = issue(79, "I79", "Prerequisite")
    transport = FakeTransport(
        [[epic]],
        {78: [{"number": 79, "node_id": "I79"}]},
        direct={79: dependency},
        children={77: [child]},
    )
    snapshot = GitHubBacklogSource(transport).discover(
        "acme/app", bound=ReadBound.EXACT, epic=77
    )
    assert snapshot.complete and snapshot.authorizable
    assert snapshot.members == ("github:7:I78",)
    assert set(snapshot.issues) == {"github:7:I77", "github:7:I78", "github:7:I79"}
    assert snapshot.selects("github:7:I78", IntakePolicy("acme/app", 7))
    assert not snapshot.selects("github:7:I79", IntakePolicy("acme/app", 7))
