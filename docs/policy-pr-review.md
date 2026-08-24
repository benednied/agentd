# Leitfaden für wirksame Pull-Request-Reviews

Die ausgewerteten Quellen ergeben ein bemerkenswert konsistentes Bild: Ein gutes Code Review ist **weder ein Style-Audit noch die Suche nach theoretisch perfektem Code**. Es ist ein risikoorientierter Engineering-Prozess, der sicherstellen soll, dass eine Änderung sinnvoll, korrekt, verständlich, testbar, wartbar und für die Codebasis insgesamt gesund ist. Gleichzeitig soll der Review-Prozess den Autor nicht unnötig blockieren und Wissen im Team verbreiten. Google formuliert dafür den stärksten übergeordneten Maßstab: Eine Änderung sollte freigegeben werden, sobald sie die Codebasis insgesamt verbessert, auch wenn noch kleinere Verbesserungsmöglichkeiten existieren. Microsoft, GitLab und Mozilla operationalisieren diesen Gedanken mit konkreten Prüfschritten; Michael Lynch und Shopify ergänzen vor allem die soziale und kommunikative Seite des Reviews. citeturn5view3turn2view8turn3view2turn6view5turn2view6turn3view12

Im Folgenden steht **PR** als Sammelbegriff für Pull Request, Merge Request beziehungsweise Googles Change List.

## Was die Quellen gemeinsam empfehlen

Die sechs Quellen erfüllen unterschiedliche Funktionen und sind deshalb eher komplementär als konkurrierend.

| Quelle | Besonders wertvoll für | Zentrale Aussage für eine interne Guideline |
|---|---|---|
| **Google Engineering Practices** | Review-Standard, Prüfreihenfolge, Kommentarregeln, Review-Navigation, Konflikte, kleine Änderungen | Code Health vor Perfektion; Design zuerst; technische Argumente vor Präferenzen; Kommentar-Schwere explizit machen. citeturn5view3turn2view1turn5view0 |
| **Michael Lynch** | Verhalten des Reviewers und Vermeidung unnötiger Reibung | Mechanisches automatisieren; zuerst High-Level-Probleme behandeln; Feedback als Anfrage statt Befehl formulieren; Prinzipien statt Geschmack diskutieren. citeturn2view6turn3view9 |
| **Microsoft Engineering Fundamentals** | Direkt umsetzbarer Enterprise-Prozess | Erst Design-Pass, dann Code-Quality-Pass; Scope diszipliniert behandeln; Fehlerbehandlung, Security und Tests explizit prüfen. citeturn3view0turn3view1turn6view4 |
| **GitLab** | Rollen, Verantwortlichkeit und Domain Expertise | Reviewer prüfen die konkrete Lösung; Maintainer beziehungsweise Code Owner schützen langfristige Architektur, Qualität und Konsistenz; bei Bedarf Spezialisten einbeziehen. citeturn3view2turn3view3 |
| **Mozilla / Firefox** | „Social contract“, Produkt- und Frontend-Risiken | Review als Gespräch verstehen; „necessary and sufficient“ prüfen; Progress gegen Perfektion abwägen; Accessibility, Performance, Security und Lokalisierung bei relevanten Änderungen berücksichtigen. citeturn4search1turn6view5turn6view6 |
| **Shopify** | Feedbackqualität, Mentoring und Teamkultur | Kontext und Grund des Feedbacks verständlich machen, Fragen stellen, Positives verstärken und Feedback so formulieren, dass es tatsächlich aufgenommen werden kann. citeturn3view12turn2view11 |

Daraus ergibt sich ein brauchbarer Grundsatz für eine interne Engineering-Guideline:

> **Review the design before the implementation. Review correctness and risk before style. Prefer simple code over speculative abstractions. Require appropriate tests for changed behaviour. Let automation enforce mechanical rules. Explain why a change is needed. Distinguish required changes from suggestions. Comment on the code, not the author. Do not block a PR merely because you would have written it differently. Approve once the change is healthy enough to improve the codebase without unresolved material risk.**

Diese Formulierung ist eine Synthese, keine wörtliche Regel einer einzelnen Quelle. Sie verbindet insbesondere Googles Code-Health-Standard mit Lynchs Kommunikationsregeln, Microsofts zweistufigem Review, Mozillas Progress-over-Perfection-Ansatz und GitLabs Verantwortungsmodell. citeturn5view3turn2view6turn3view1turn6view5turn3view2

Wichtig ist dabei eine methodische Einschränkung: Bei diesen Quellen handelt es sich primär um Engineering-Practice-Guides und Erfahrungsberichte großer Softwareorganisationen, nicht um kontrollierte Vergleichsstudien. Die folgende Guideline ist daher am besten als **konsolidierter Praxisstandard** zu verstehen, nicht als Beweis dafür, dass jedes einzelne Verfahren unter allen Umständen kausal die niedrigste Fehlerrate produziert. citeturn1search6turn0search14turn2view9turn2view10turn2view6turn2view11

## Die Leitprinzipien eines guten Reviews

**Code Health ist der Maßstab, nicht Perfektion.** Ein Reviewer sollte keine Änderung für beliebig kleine Verbesserungswünsche festhalten. Google empfiehlt ausdrücklich, eine Änderung freizugeben, sobald sie den Gesamtzustand der Codebasis verbessert, auch wenn sie nicht perfekt ist. Mozilla formuliert denselben Trade-off als Balance zwischen hohen Standards und Fortschritt. Das bedeutet nicht, bekannte relevante Qualitätsprobleme durchzuwinken; es bedeutet, zwischen materiellen Problemen und bloßer Optimierung zu unterscheiden. citeturn5view3turn6view5

**Design kommt vor Implementierungsdetails.** Google bezeichnet das Gesamtdesign als wichtigsten Gegenstand eines Reviews und empfiehlt, zuerst die Hauptteile einer Änderung zu betrachten. Ein fundamentaler Designfehler sollte früh zurückgemeldet werden, bevor Reviewer und Autor Zeit mit Details verbringen, die nach einer Neugestaltung ohnehin verschwinden. Microsoft spiegelt das mit einem expliziten „First Design Pass“ vor dem „Code Quality Pass“. Lynch empfiehlt ebenfalls, vom High Level zu den Details zu arbeiten. citeturn2view1turn2view3turn3view1turn2view6

**Korrektheit und Risiko kommen vor Stil.** Der menschliche Reviewer sollte seine begrenzte Aufmerksamkeit vor allem auf Geschäftslogik, Design, Tests, Fehlerfälle und andere semantische Risiken verwenden. Microsoft empfiehlt ausdrücklich, automatisierbare Teile an Linters und ähnliche Werkzeuge abzugeben. Lynch argumentiert in dieselbe Richtung für Formatter und andere mechanische Prüfungen; Mozilla schlägt sogar vor, wiederkehrende Review-Probleme in neue Lint-Regeln zu überführen. citeturn2view8turn2view6turn6view5

**Einfachheit ist eine Qualitätsanforderung.** Google warnt insbesondere vor unnötiger Komplexität und spekulativem Over-Engineering: Eine Abstraktion sollte ein reales Problem lösen und nicht nur ein hypothetisches zukünftiges. Mozilla weist ergänzend darauf hin, dass eine Wiederverwendungsschicht beim ersten einzelnen Consumer häufig verfrüht ist. Microsoft fragt im Quality Pass unter anderem, ob Funktionen beziehungsweise Klassen zu komplex sind und ob unnötige Funktionalität eingeführt wurde. citeturn6view0turn3view6turn3view1

**Tests gehören zur Änderung.** Google erwartet geeignete Unit-, Integrations- oder End-to-End-Tests grundsätzlich im selben Change. Microsoft formuliert denselben Grundsatz noch strenger und fordert Tests im selben PR; Mozilla erlaubt explizite Ausnahmen, etwa für bestimmte reine Style-, Lokalisierungs-, Refactoring- oder schwer automatisierbare Änderungen. Daraus folgt als vernünftiger gemeinsamer Standard: **Geändertes Verhalten braucht angemessene Tests im selben PR, sofern nicht eine nachvollziehbare Ausnahme dokumentiert ist.** citeturn6view0turn6view4turn3view4

**Review ist keine Geschmacksabstimmung.** Google priorisiert technische Fakten und Engineering-Prinzipien vor persönlicher Präferenz. Für reine Stilfragen ist der definierte Style Guide maßgeblich; gibt es keine Regel und mehrere technisch gleichwertige Lösungen, sollte der Reviewer die Präferenz des Autors akzeptieren. Lynch empfiehlt entsprechend, Stilfragen außerhalb einzelner Reviews dauerhaft durch Style Guides zu entscheiden. citeturn5view3turn2view6

**Review-Kommentare müssen ihre Bedeutung erkennen lassen.** Google empfiehlt ausdrücklich, erforderliche Änderungen von Guidelines, Vorschlägen und rein informativen Hinweisen zu unterscheiden, zum Beispiel mittels `Nit:`, `Optional:` beziehungsweise `Consider:` und `FYI:`. Mozilla fordert ebenfalls Klarheit darüber, welche Änderungen für die Freigabe erforderlich und welche optional sind. citeturn5view0turn6view5

**Der Gegenstand der Kritik ist der Code, nicht der Autor.** Google, Microsoft, Mozilla und Lynch empfehlen nahezu identisch, Formulierungen zu vermeiden, die eine technische Entscheidung als persönliches Versagen des Autors erscheinen lassen. Shopify ergänzt, dass Review nicht als Bewertung der Fähigkeiten einer Person behandelt werden sollte, sondern als kollaborative Verbesserung von Code und Team. citeturn2view2turn3view0turn6view5turn3view8turn3view12

**Gutes Verhalten sollte ebenfalls sichtbar gemacht werden.** Google, Lynch, Microsoft, Mozilla und Shopify empfehlen positive Kommentare, wenn beispielsweise eine Lösung besonders klar, eine Testabdeckung gut oder eine Vereinfachung gelungen ist. Das ist nicht bloß Höflichkeit: Review wird dadurch zugleich zum Mechanismus für Wissenstransfer und Verstärkung erwünschter Engineering-Praktiken. citeturn2view2turn3view10turn3view0turn6view5turn3view12

## Der empfohlene Review-Ablauf

Eine der wichtigsten Erkenntnisse aus den Quellen ist, dass ein Review **nicht einfach Datei für Datei von oben nach unten beginnen sollte**. Google und Microsoft empfehlen im Kern einen mehrstufigen Ablauf: erst Kontext und Design verstehen, danach Verhalten und Risiko, anschließend Codequalität und erst zuletzt mechanische Details. citeturn2view3turn3view1

| Phase | Was der Reviewer tut | Warum |
|---|---|---|
| **Readiness** | Beschreibung, Ziel, Scope und CI-Status prüfen. Unklare Absicht, offensichtlich vermischte Aufgaben oder grundlegende Build-/Testprobleme zuerst klären. | Verhindert detailliertes Review eines PRs, dessen Zweck oder Ausgangszustand noch nicht belastbar ist. citeturn2view3turn3view1turn3view4turn3view2 |
| **Intent & Scope** | Verstehen, welches Problem gelöst werden soll und ob sämtliche Änderungen dafür notwendig sind. Ebenso prüfen, ob etwas für eine vollständige Lösung fehlt. | Mozilla beschreibt dies prägnant als Prüfung auf „necessary“ und „sufficient“. Unrelated cleanup gehört normalerweise in einen separaten PR. citeturn3view5turn3view0 |
| **Design** | Hauptdateien beziehungsweise zentrale Abstraktionen zuerst lesen. Architektur, Verantwortlichkeiten, Interfaces, Datenfluss und Integration in das bestehende System beurteilen. | Grundlegende Designprobleme machen Detailfeedback unter Umständen hinfällig. citeturn2view1turn2view3turn3view1 |
| **Functionality & Risk** | Happy Path und Fehlerfälle durchdenken; Nutzerwirkung, Randfälle, Zustandsübergänge, Concurrency, Security, Privacy und gegebenenfalls Performance prüfen. | Code kann Tests bestehen und dennoch falsches Verhalten, Race Conditions oder systemische Risiken enthalten. citeturn2view1turn6view4turn6view6 |
| **Maintainability** | Komplexität, Verständlichkeit, Naming, Abstraktionsniveau, Fehlerbehandlung und Konsistenz untersuchen. | Der PR wird nicht nur heute ausgeführt, sondern zukünftig gelesen, verändert und debuggt. citeturn6view0turn6view4 |
| **Tests** | Nicht nur feststellen, dass Tests existieren, sondern prüfen, ob sie relevante Verhaltensweisen und Edge Cases tatsächlich absichern und bei einem Defekt sinnvoll fehlschlagen würden. | Google weist ausdrücklich darauf hin, dass auch Tests menschlich auf Sinnhaftigkeit und Korrektheit geprüft werden müssen. citeturn6view0 |
| **Documentation & Style** | Kommentare, öffentliche Dokumentation, README/API-Dokumentation, Style Guide und automatisierte Regeln prüfen. | Diese Themen sind wichtig, sollten aber nicht die Prüfung von Design und Korrektheit verdrängen. citeturn6view1turn6view2 |
| **Decision** | Prüfen, ob materielle Einwände erledigt sind; verbleibende kleine oder optionale Punkte entsprechend kennzeichnen; freigeben, sobald kein relevanter Grund mehr zum Blockieren besteht. | Zusätzliche Review-Runden allein wegen trivialer oder optionaler Punkte erzeugen unnötige Latenz. citeturn5view3turn3view10 |

### Readiness: Bevor der eigentliche Review beginnt

Der Reviewer sollte zunächst die PR-Beschreibung und das Ziel der Änderung verstehen können. Google empfiehlt, zunächst zu prüfen, ob die Änderung überhaupt sinnvoll ist und danach den wichtigsten Teil des Changes zu betrachten. Microsoft fragt in seinem ersten Pass, ob die Beschreibung verständlich ist und ob alle enthaltenen Änderungen logisch zusammengehören. Mozilla erwartet unter anderem einen verständlichen Commit-Kontext, passende Tests und erfolgreiche automatisierte Prüfungen. GitLab fordert vor dem Maintainer-Review grundsätzlich erfolgreiche Tests oder eine Erklärung für bekannte Fehlschläge. citeturn2view3turn3view1turn3view4turn3view2

Das führt zu einer wichtigen internen Regel:

> **Do not spend expensive human review time on problems that the author, CI, formatter, linter or static analysis could have found before review.**

Die Regel sollte allerdings nicht bedeuten, dass ein Reviewer bei jedem roten CI-Job blind stoppt. Ein bekannter infrastruktureller oder unabhängiger Fehler kann dokumentiert werden; entscheidend ist, dass der Status transparent und nicht stillschweigend dem Reviewer zur Diagnose überlassen wird. GitLab berücksichtigt genau diese Möglichkeit, indem fehlgeschlagene Tests erklärt werden sollen. citeturn3view2

### Scope: Ist der PR notwendig und hinreichend?

Mozillas Gegensatz „necessary and sufficient“ ist besonders gut als Review-Heuristik geeignet. **Necessary** bedeutet: Alles, was der PR ändert, sollte zur Aufgabe gehören. **Sufficient** bedeutet: Die Änderung muss das eigentliche Problem vollständig lösen, einschließlich relevanter Edge Cases, statt nur ein sichtbares Symptom zu beseitigen. citeturn3view5

Damit lässt sich ein häufiges Review-Antipattern vermeiden: Der Reviewer entdeckt im Umfeld einer Änderung alte technische Schulden und beginnt, deren Beseitigung als Voraussetzung für den aktuellen PR zu verlangen. Microsoft fordert ausdrücklich, Probleme außerhalb des PR-Scopes als separate Tasks zu behandeln und den laufenden PR nicht allein deshalb zu blockieren. Google unterscheidet ähnlich zwischen neu eingeführter Komplexität, die vor dem Merge bereinigt werden sollte, und bereits bestehenden umliegenden Problemen, für die ein Follow-up erfasst werden kann. citeturn3view0turn6view7

Die praktische Regel lautet daher:

> **Block on problems introduced by the PR, required for the PR to be correct, or made materially worse by the PR. Track unrelated pre-existing debt separately.**

### Design: Erst das System, dann die Zeile

Google empfiehlt, vor der line-by-line-Prüfung die zentrale Änderung zu finden und auf Systemebene zu verstehen. Stellt sich dort beispielsweise heraus, dass eine neue Abstraktion an der falschen Systemgrenze sitzt oder dass der gesamte Ansatz unnötige Komplexität erzeugt, sollte dieses Feedback sofort gegeben werden. Microsoft behandelt denselben Bereich als separaten Design-Pass. citeturn2view3turn3view1

Fragen für diesen Pass sind insbesondere:

| Designfrage | Worauf sie zielt |
|---|---|
| Gehört diese Funktionalität an diese Stelle? | Verantwortlichkeit und Architektur |
| Passen die neuen Komponenten zu den bestehenden Systemgrenzen? | Integrationsfähigkeit |
| Ist eine neue Abstraktion tatsächlich notwendig? | Vermeidung von Over-Engineering |
| Entsteht eine unnötige Kopplung zwischen Komponenten? | Änderbarkeit und Testbarkeit |
| Wird ein bestehendes Pattern sinnvoll wiederverwendet? | Konsistenz |
| Ist die Lösung größer oder allgemeiner als das bekannte Problem? | YAGNI und Komplexitätskontrolle |

Diese Fragen kombinieren Googles Fokus auf Design und Over-Engineering mit Microsofts Prüfung von Architekturmustern und Mozillas Warnung vor verfrühter Generalisierung. citeturn2view1turn6view0turn3view1turn3view6

### Functionality & Risk: Nicht nur fragen, ob der Happy Path funktioniert

Google empfiehlt ausdrücklich, Edge Cases und insbesondere Concurrency-Probleme wie Race Conditions oder Deadlocks gedanklich zu prüfen. Microsoft ergänzt Error Handling, Security, mögliche Systemeffekte und Datenschutzfragen. Bei Nutzeroberflächen empfiehlt Google gegebenenfalls eine tatsächliche Validierung oder Demo statt ausschließlich Diff-Lektüre. citeturn2view1turn6view4

Eine generische interne Guideline sollte deshalb bei risikorelevanten PRs zusätzlich fragen:

| Risikodimension | Review-Fragen |
|---|---|
| **Correctness** | Was passiert bei ungültigen, leeren, maximalen oder unerwarteten Eingaben? |
| **Failure handling** | Werden Timeouts, Exceptions, partielle Fehler und Retry-Situationen korrekt behandelt? |
| **State** | Können inkonsistente Zwischenzustände entstehen? |
| **Concurrency** | Gibt es Race Conditions, Deadlocks, doppelte Verarbeitung oder verlorene Updates? |
| **Security** | Werden Trust Boundaries, Validierung, Autorisierung und sensible Daten korrekt behandelt? |
| **Privacy** | Werden personenbezogene oder sensible Informationen unnötig gespeichert oder geloggt? |
| **Performance** | Erzeugt die Änderung neue Datenbank-/Netzwerkaufrufe, Hot-Path-Arbeit oder pathologische Skalierung? |
| **User-facing behaviour** | Funktioniert der tatsächliche Workflow, nicht nur die isolierte Funktion? |
| **Accessibility / i18n** | Sind relevante UI-Änderungen tastaturbedienbar, semantisch korrekt und lokalisierbar? |

Nicht jede Änderung benötigt für jede Dimension eine umfassende Spezialprüfung. Google empfiehlt vielmehr, bei komplexen Spezialgebieten wie Security, Privacy, Concurrency, Accessibility oder Internationalisierung einen qualifizierten Reviewer hinzuzuziehen; GitLab formalisiert dasselbe Prinzip über Domain Experts. Mozilla zeigt für Frontend-Änderungen, wie solche domänenspezifischen Prüfkriterien konkret aussehen können. citeturn6view3turn3view3turn6view6

### Maintainability: Der Reviewer vertritt auch zukünftige Leser

Google legt großen Wert darauf, dass Code schnell verständlich bleibt. Wenn selbst ein qualifizierter Reviewer eine Änderung trotz ausreichendem Kontext kaum verstehen kann, ist dies selbst ein Wartbarkeitssignal. Erklärungen ausschließlich im Review-Thread lösen dieses Problem nicht, weil zukünftige Leser diese Erklärung am Code nicht sehen. In einem solchen Fall sollte bevorzugt der Code vereinfacht oder die notwendige Begründung im Code beziehungsweise in dauerhafter Dokumentation hinterlegt werden. citeturn6view0turn5view0

Dabei sollten Kommentare primär **Begründungen und Kontext** liefern, die aus dem Code selbst nicht hervorgehen. Ein Kommentar, der lediglich den unmittelbar sichtbaren Ablauf des Codes in Prosa wiederholt, ist häufig ein Hinweis darauf, dass Naming oder Struktur verbessert werden können. Google, GitLab und Mozilla formulieren diesen Gedanken sehr ähnlich. citeturn6view1turn3view2turn4search1

### Tests: Testcode wird mitreviewt

Die bloße Existenz eines Tests ist kein ausreichendes Akzeptanzkriterium. Google empfiehlt zu prüfen, ob der Test tatsächlich fehlschlägt, wenn das getestete Verhalten defekt ist, ob die Assertions sinnvoll sind und ob der Test selbst unnötig komplex ist. Microsoft fordert ebenfalls sinnvolle Tests und die Berücksichtigung von Edge Cases. citeturn6view0turn6view4

Eine gute Review-Frage lautet daher nicht nur:

> „Gibt es einen Test?“

sondern:

> **„Welchen konkreten Defekt würde dieser Test erkennen, und würde er wirklich rot werden, wenn dieser Defekt auftritt?“**

Tests können außerdem als Einstieg in einen komplexen PR dienen. Sowohl Google als auch Microsoft erwähnen, dass das Lesen der Tests vor der Implementation helfen kann, das erwartete Verhalten der Änderung zu verstehen. citeturn2view3turn6view4

## Wie Review-Kommentare geschrieben werden sollten

Die kommunikative Qualität des Reviews ist in diesen Quellen kein „Soft Skill“-Anhang, sondern Teil der Engineering-Qualität. Ein technisch korrekter Einwand, dessen Bedeutung unklar bleibt oder der unnötige Abwehrreaktionen hervorruft, erzeugt mehr Iterationen und weniger gemeinsames Verständnis. Google fordert Höflichkeit, Begründungen und Klarheit; Microsoft und Mozilla empfehlen Fragen beziehungsweise nicht-personalisierte Formulierungen; Lynch behandelt Review ausdrücklich als Situation mit erhöhtem Konfliktpotenzial; Shopify betont, dass Feedback so formuliert werden muss, dass es tatsächlich aufgenommen werden kann. citeturn2view2turn3view0turn6view5turn3view9turn2view11

### Bedeutung vor Tonfall

Der wichtigste Schritt ist nicht, jeden Kommentar maximal weich zu formulieren, sondern **seine Absicht und Verbindlichkeit unmissverständlich zu machen**.

Google empfiehlt für nicht verpflichtende Kommentare:

| Präfix | Bedeutung |
|---|---|
| `Nit:` | Kleine Politur beziehungsweise geringfügiger Punkt; nicht geeignet, allein den Merge zu blockieren. |
| `Optional:` / `Consider:` | Verbesserungsidee, deren Umsetzung nicht Voraussetzung für die Freigabe ist. |
| `FYI:` | Information oder Lernhinweis ohne erwartete Änderung im aktuellen PR. |

Google empfiehlt diese Kennzeichnung gerade deshalb, weil Autoren sonst dazu neigen können, sämtliche Kommentare als verpflichtend zu interpretieren. citeturn5view0

Für eine interne Guideline würde ich dieses Schema um zwei explizite Stufen ergänzen:

| Präfix | Interne Bedeutung | Merge-blockierend? |
|---|---|---|
| `Blocking:` | Materielles Problem, das Correctness, Security, Datenintegrität oder fundamentale Architektur betrifft. | Ja |
| `Required:` | Änderung wird für die Freigabe verlangt, auch wenn sie kein Produktions-Blocker im engeren Sinn ist. | Ja |
| `Nit:` | Kleine Qualitäts- oder Polituränderung. | Nein |
| `Optional:` / `Consider:` | Alternative oder Verbesserungsvorschlag. | Nein |
| `FYI:` | Information für Kontext oder zukünftige Arbeit. | Nein |

`Blocking:` und `Required:` sind hier eine **bewusste Erweiterung** der Google-Taxonomie. Sie lösen dasselbe Problem in der anderen Richtung: Nicht nur optionale Hinweise, sondern auch tatsächliche Freigabevoraussetzungen sollten sichtbar sein. Google und Mozilla fordern die Unterscheidung von verpflichtendem und optionalem Feedback ausdrücklich. citeturn5view0turn6view5

### Gute Kommentare erklären Ursache und Konsequenz

Ein schwacher Kommentar lautet:

> `Required: Change this.`

Ein besserer Kommentar lautet beispielsweise:

> `Required:` This branch silently treats a timeout as success, so callers can persist an incomplete result. Could we propagate the timeout here and cover that path with a test?

Der zweite Kommentar enthält vier Informationen: **Schweregrad, Beobachtung, Konsequenz und gewünschte Richtung**. Damit muss der Autor nicht erraten, ob der Reviewer einen Stylewunsch äußert, einen Bug vermutet oder eine konkrete Invariante schützen will. Google empfiehlt ausdrücklich, den Grund für Änderungen zu erklären; Microsoft bevorzugt ebenfalls eine Begründung und gegebenenfalls ein Beispiel. citeturn2view2turn3view0

### Auf den Code beziehen, nicht auf die Person

Statt:

> Why did you implement this with two caches?

besser:

> `Question:` What benefit does the second cache provide here? It looks as though the existing cache already covers this access pattern.

Oder, wenn die Änderung definitiv notwendig ist:

> `Required:` The second cache introduces another source of invalidation state. Can we use the existing cache instead?

Google zeigt genau diesen Unterschied zwischen personenbezogener und codebezogener Kritik; Microsoft, Mozilla und Lynch empfehlen ebenfalls, „you“-orientierte beziehungsweise anklagende Formulierungen zu vermeiden. citeturn2view2turn3view0turn6view5turn3view8

### Fragen verwenden, ohne Anforderungen zu verschleiern

Lynch, Microsoft, Mozilla und Shopify empfehlen Fragen als kooperatives Kommunikationsmittel. Das ist besonders sinnvoll, wenn dem Reviewer Kontext fehlen könnte oder mehrere Lösungen möglich sind. citeturn3view9turn3view0turn6view5turn3view12

Allerdings folgt aus der ebenso starken Forderung nach eindeutiger Severity eine wichtige Synthese: **Eine zwingende Änderung sollte nicht als scheinbar optionale Frage versteckt werden.**

Bei echter Unsicherheit:

> `Question:` Could this callback run after the object has been disposed?

Bei einem bereits identifizierten Defekt:

> `Blocking:` This callback can run after disposal and dereference released state. Please guard or cancel it before destruction.

Freundlichkeit und Klarheit stehen nicht im Gegensatz zueinander. Ein Review kann respektvoll und zugleich eindeutig sein.

### Beispiele gezielt einsetzen

Lynch empfiehlt konkrete Codebeispiele, wenn sie eine Verbesserung schnell verständlich machen, warnt jedoch davor, dem Autor den gesamten PR umzuschreiben. Beispiele eignen sich besonders für kleine, unstrittige Verbesserungen oder um eine abstrakte Idee greifbar zu machen. citeturn3view8

Zum Beispiel:

```text
Optional: This might be simpler as an early return:

if (!user) {
    return;
}

process(user);
```

Das Beispiel zeigt die gedachte Richtung, ohne dem Autor bei einer größeren Designentscheidung eine vollständige Implementierung vorzuschreiben.

### Positives Feedback ist Teil des Reviews

Ein Review sollte nicht ausschließlich aus Defekten bestehen. Google empfiehlt, besonders gute Tests, Vereinfachungen oder andere starke Engineering-Entscheidungen explizit hervorzuheben. Lynch, Mozilla, Microsoft und Shopify vertreten denselben Ansatz. citeturn2view2turn3view10turn6view5turn3view0turn3view12

Ein sinnvoller positiver Kommentar benennt dabei ebenfalls den Grund:

> Nice simplification. Keeping the retry policy in one place makes the failure behaviour much easier to reason about.

So wird aus Lob ein Wissenstransfer darüber, **welche Eigenschaft** der Lösung erwünscht ist.

## Scope, Geschwindigkeit, Rollen und Konflikte

### Kleine, kohärente PRs sind ein Qualitätswerkzeug

Google argumentiert besonders ausführlich für kleine Changes: Sie können schneller und gründlicher geprüft werden, sind leichter zu verstehen, bergen weniger Bug-Risiko, verschwenden bei einer verworfenen Designrichtung weniger Arbeit, verursachen weniger Merge-Konflikte und lassen sich einfacher zurückrollen. Ein Change sollte möglichst eine klar abgegrenzte Sache inklusive zugehöriger Tests adressieren. citeturn2view5

Microsoft erwartet entsprechend kompakte PRs zu klar definierten Tasks; Mozilla stellt ebenfalls fest, dass mehrere kleine Patches leichter zu prüfen sind als eine große monolithische Änderung. citeturn0search10turn4search1

Daraus sollte allerdings **keine starre Lines-of-Code-Grenze** entstehen. Die entscheidende Größe ist die kognitive und logische Einheit des Changes. Ein automatisiert erzeugter 1.000-Zeilen-Change kann trivial sein, während 50 Zeilen verteilte Concurrency-Logik extrem anspruchsvoll sein können. Die Quellen argumentieren primär für Verständlichkeit und fokussierten Scope, nicht für eine universelle numerische Grenze. citeturn2view5turn2view3

Ein PR sollte deshalb typischerweise:

**eine kohärente Absicht**, **keine unabhängigen Refactorings oder Formatierungsaktionen**, **die für sein Verhalten notwendigen Tests** und **die unmittelbar erforderliche Dokumentation** enthalten. Große funktionale und großflächige rein stilistische Änderungen sollte man trennen, weil Letztere das eigentliche Diff schwerer lesbar machen. citeturn2view5turn6view2turn3view5

### Review-Latenz ist ein Engineering-Problem

Google betrachtet Review-Geschwindigkeit als wichtig, ohne dafür Qualitätsstandards aufzugeben. Ist ein Change zu groß, sollte er normalerweise aufgeteilt werden; lässt er sich nicht sinnvoll teilen, empfiehlt Google zumindest frühes Feedback zum Gesamtdesign, damit der Autor nicht vollständig blockiert bleibt. Lynch geht noch weiter und empfiehlt, Reviews sehr früh zu beginnen, gerade weil langsame Reviews Autoren von kleinen Changes abhalten können. citeturn2view4turn3view7

Die sinnvollste interne Umsetzung ist **kein universelles „innerhalb von X Minuten“**, sondern ein klarer Team-Standard für die erste Reaktion. GitLab operationalisiert diese Idee über ein Review-Response-SLO und verlangt bei Nichtverfügbarkeit die Übergabe an einen anderen Reviewer. citeturn3view2

Damit wird Review-Latenz sichtbar als Teil des Delivery-Systems behandelt und nicht als persönliche Gefälligkeit des Reviewers.

### Reviewer müssen den ihnen zugewiesenen Code verstehen

Google und Microsoft erwarten grundsätzlich, dass der Reviewer die tatsächlich zugewiesenen menschlich geschriebenen Änderungen liest und versteht. Reicht der Diff-Kontext nicht aus, sollte die gesamte Datei beziehungsweise das umliegende System betrachtet werden. Google weist außerdem darauf hin, dass ein Spezialreviewer hinzugezogen werden sollte, wenn dem Reviewer für einen relevanten Bereich die erforderliche Expertise fehlt. citeturn6view2turn2view8turn6view3

Bei mehreren Reviewern sollte deshalb klar sein, **wer welchen Bereich abdeckt**. GitLab empfiehlt ausdrücklich, beim Zuweisen mehrerer Reviewer deren jeweilige Review-Domäne kenntlich zu machen. Google empfiehlt analog, bei Teilreviews anzugeben, welche Bereiche tatsächlich geprüft wurden. citeturn3view2turn6view3

Das verhindert ein gefährliches Zuständigkeitsvakuum, bei dem jeder Reviewer annimmt, jemand anderes habe beispielsweise Security oder Datenmigrationen geprüft.

### Der Autor bleibt für die Lösung verantwortlich

Google weist darauf hin, dass der Reviewer nicht verpflichtet ist, die Lösung für den Autor zu entwerfen oder den Code selbst zu schreiben. Gute Reviewer helfen, geben gegebenenfalls Hinweise oder Beispiele, aber die Problemlösung bleibt grundsätzlich Aufgabe des Autors. citeturn2view2

Damit sollte ein Reviewer weder in das Extrem „Hier stimmt etwas nicht, finde selbst heraus was“ noch in das andere Extrem „Ich implementiere den PR über Review-Kommentare neu“ fallen. Sinnvoll ist ein abgestufter Ansatz: Problem und Grund benennen; bei Bedarf eine Richtung aufzeigen; konkrete Implementierung vor allem dann vorschlagen, wenn sie eindeutig und hilfreich ist. citeturn2view2turn3view8

### Review ist geteilter Code-Ownership

GitLabs Rollenmodell liefert hierfür eine nützliche Sichtweise. Dort prüft der Reviewer primär die konkrete Lösung, während Maintainer für übergreifende Gesundheit, Architektur, Organisation, Separation of Concerns, Tests, Konsistenz und Lesbarkeit der Codebasis verantwortlich sind. Mit einer Freigabe übernehmen Maintainer Verantwortung gemeinsam mit dem Autor. citeturn3view2

Nicht jedes Team benötigt diese formale Trennung. Das zugrunde liegende Prinzip ist aber allgemein brauchbar:

> **Approval is an engineering decision, not an acknowledgement that comments have been answered.**

Der Reviewer bestätigt mit der Freigabe, dass er die Änderung in dem von ihm verantworteten Umfang für akzeptabel hält.

### Meinungsverschiedenheiten werden über Prinzipien entschieden

Google empfiehlt bei Widerspruch zunächst ernsthaft zu prüfen, ob der Autor recht hat. Der Autor hat sich häufig länger mit dem konkreten Code beschäftigt und kann Kontext besitzen, der dem Reviewer fehlt. Überzeugt die technische Begründung, sollte der Reviewer den Punkt fallenlassen. Andernfalls sollte er seinen Einwand genauer anhand von Code Health, Daten oder Engineering-Prinzipien begründen. citeturn6view7

Das verhindert eine häufige Fehlinterpretation von Review-Autorität: **Reviewer zu sein bedeutet nicht automatisch, in jeder technischen Frage recht zu haben.**

Bei rein persönlicher Präferenz sollte dagegen normalerweise der Autor entscheiden, sofern die Lösung den Standards entspricht. Google priorisiert technische Fakten und festgelegte Standards ausdrücklich über individuelle Vorlieben. citeturn5view3

Bleibt nach wenigen Kommentar-Runden ein echter Konflikt bestehen, empfehlen Google und Microsoft, die Diskussion aus dem asynchronen Thread in ein kurzes Gespräch zu verlagern und das Ergebnis anschließend wieder im PR zu dokumentieren. Ist weiterhin keine Einigung möglich, sollte ein Tech Lead, Maintainer oder anderer geeigneter Entscheider einbezogen werden, statt den PR unbegrenzt im Review festhängen zu lassen. Mozilla empfiehlt ebenfalls direkte Gespräche, wenn sie schneller zur Klärung führen, und die anschließende Dokumentation für zukünftige Leser. citeturn5view3turn3view0turn4search1

### Wann freigegeben werden sollte

Hier ist die Quellenlage besonders klar. Google warnt vor Perfektionismus und empfiehlt Approval, sobald der Change die Codebasis insgesamt verbessert und keine wichtigen Qualitätsprobleme mehr bestehen. Lynch empfiehlt, Approval nicht allein deshalb zurückzuhalten, weil nur noch triviale Korrekturen oder ausdrücklich optionale Vorschläge offen sind. Mozilla kennt ebenfalls die Möglichkeit, eine Änderung trotz kleiner verbleibender Punkte bereits zu genehmigen, wenn der Reviewer dem Autor deren korrekte Umsetzung ohne zusätzliche Runde zutraut. citeturn5view3turn3view10turn4search1

Für eine interne Guideline ergibt sich daraus:

> **Approve when all blocking and required concerns are resolved and the remaining feedback is only minor, optional or informational. Do not demand another review round merely to observe trivial edits unless those edits could materially change behaviour or risk.**

Das ist deutlich strenger als „approve, wenn die Tests grün sind“, aber deutlich pragmatischer als „approve erst, wenn der Reviewer überhaupt nichts mehr verbessern würde“.

## Direkt einsetzbare interne Review-Guideline

Die folgende Version verdichtet die Quellen zu einer policy-artigen Fassung, die weitgehend unabhängig von GitHub, GitLab, Gerrit oder Azure DevOps funktioniert.

### Purpose

**Code review protects and improves the long-term health of the codebase while enabling engineers to make progress.** Review is not a search for perfect code and not an opportunity to rewrite a change according to the reviewer’s personal preferences. citeturn5view3turn6view5

### Review order

**Review from high level to low level.** Start with the purpose and scope of the change, then review design, correctness and risk, maintainability and tests, and only afterwards naming, comments, documentation and style. Raise major design concerns as early as possible rather than completing a detailed review of code that may need to be redesigned. citeturn2view3turn3view1

### Scope

**A PR should represent one coherent change.** Do not require unrelated cleanup as a condition for approval. Problems introduced by the PR, or required to make the PR correct and maintainable, belong in the current change; unrelated existing technical debt should normally become separate work. citeturn2view5turn3view0turn3view5

### Design and complexity

**Prefer the simplest design that solves the known problem.** Challenge unnecessary abstractions, speculative generality, duplicated responsibilities and complexity that makes the code harder to understand or change. Do not demand abstractions solely for hypothetical future requirements. citeturn6view0turn3view6

### Correctness and risk

**Review behaviour, not only syntax.** Consider edge cases, error paths, state transitions, concurrency, user impact and system-wide effects. Changes involving sensitive domains such as security, privacy, data integrity, accessibility, internationalization or complex concurrency should receive appropriate domain review. citeturn2view1turn6view3turn6view4

### Tests

**Changed behaviour should have appropriate tests in the same PR unless an exception is justified.** Review the tests themselves: they must verify meaningful behaviour, cover relevant failure and edge cases, and actually fail when the behaviour they protect is broken. citeturn6view0turn6view4turn3view4

### Automation and style

**Let tools enforce mechanical rules wherever practical.** Formatting, lint rules and other deterministic checks belong in automation. Style-guide requirements may be enforced in review; personal stylistic preferences must not block an otherwise healthy change. Repeated mechanical review comments are candidates for new automated rules. citeturn2view6turn5view3turn6view5

### Comments and documentation

**Prefer clear code over explanations in the review thread.** Comments in the code should primarily explain intent, constraints or reasons that cannot be expressed clearly through the implementation itself. Update durable documentation when the PR changes how users or developers build, use, test or operate the system. citeturn5view0turn6view1turn6view2

### Review feedback

**Be clear, specific and respectful. Explain why.** Comment on the code rather than the author. Use questions when context may be missing and requests rather than unnecessarily imperative language. Provide examples when they make the desired outcome substantially clearer. Call out good engineering decisions as well as problems. citeturn2view2turn3view9turn3view8turn3view12

### Severity

**Make the required action explicit.**

```text
Blocking:  Must be resolved before merge because of material correctness,
           security, data-integrity or architectural risk.

Required:  Must be resolved before merge to meet the team's engineering standard.

Nit:       Minor polish. Do not block the PR solely on this point.

Optional:  Suggested improvement; implementation is not required.

FYI:       Informational or educational; no change is expected.
```

The important rule is not the exact vocabulary but that authors can immediately distinguish merge requirements from suggestions and informational comments. citeturn5view0turn6view5

### Review turnaround

**Treat reviews as priority work and avoid unnecessary latency.** Review small changes promptly. Ask for large changes to be split where practical. If a complete review cannot be performed promptly, give useful high-level feedback early rather than silently blocking the author. Teams should define a realistic review-response expectation appropriate to their workflow. citeturn2view4turn3view7turn3view2

### Disagreement

**Resolve disagreements with facts, standards and engineering principles rather than authority or preference.** Reconsider a concern when the author provides new information. If asynchronous discussion stops being productive, talk directly, document the result in the PR and escalate to the appropriate technical owner when necessary. citeturn5view3turn6view7turn3view0

### Approval

**Approve when the PR is correct, appropriately tested, maintainable and leaves the codebase in a healthy state, even if minor improvements remain.** Do not require additional rounds solely to verify nits or optional suggestions. Approval should be withheld while unresolved blocking or required concerns remain. citeturn5view3turn3view10turn4search1

A compact merge-readiness check derived from the complete guide is therefore:

| Check | Merge-ready when… |
|---|---|
| **Purpose** | The reviewer understands what problem the PR solves. |
| **Scope** | The PR is coherent and contains no significant unrelated work. |
| **Design** | The approach fits the architecture without unnecessary complexity. |
| **Correctness** | Expected behaviour and relevant failure/edge cases are sound. |
| **Risk** | Security, privacy, concurrency, data and other relevant risks are acceptably handled. |
| **Tests** | Changed behaviour is adequately protected and the tests themselves are meaningful. |
| **Maintainability** | Future engineers can reasonably understand and modify the code. |
| **Documentation** | Durable documentation and comments contain the context future readers need. |
| **Style** | Objective project rules pass; no merge is blocked by personal taste. |
| **Feedback** | All `Blocking:` and `Required:` concerns are resolved. |
| **Approval** | Remaining comments are only `Nit:`, `Optional:` or `FYI:` and do not represent material risk. |

Diese Zusammenfassung trifft den stärksten gemeinsamen Nenner der Quellen: **Ein Reviewer schützt die Codebasis vor realen Qualitätsverschlechterungen, nicht vor jeder denkbaren Unvollkommenheit.** Der Review beginnt bei Sinn, Scope und Design, bewegt sich über Korrektheit, Risiko und Tests zu Wartbarkeit und erst danach zu mechanischen Details. Automatisierbare Regeln werden automatisiert; subjektive Präferenzen blockieren nicht. Feedback benennt Grund und Schweregrad, respektiert die Expertise des Autors und behandelt Review als gemeinsame Arbeit am Code. Kleine, fokussierte Changes und schnelle Rückmeldung reduzieren die Kosten dieses Prozesses, ohne den Qualitätsstandard zu senken. citeturn5view3turn2view3turn6view0turn2view6turn5view0turn2view5turn2view4