# Gemeinsamer Arbeitsablauf v1

**Version v1; gilt erst durch ausdrückliche Einbindung.** Dieser Ablauf ergänzt die vorhandene Entwickleranweisung des Zielprojekts. Lokale Zuständigkeiten und Releasevorgaben bleiben erhalten. Die gemeinsamen Arbeitsgrundsätze stehen in [AI.md](AI.md).

## 1. Einen prüfbaren Auftrag geben

Beschreibe das gewünschte Ergebnis, den betroffenen Bereich und woran die Fertigstellung erkennbar ist. Benenne besondere Grenzen wie „nur lesen“, „keine Tests“, „kein Deployment“ oder eine ausdrücklich erlaubte Veröffentlichung. Bereits erteilte Befugnisse gelten weiter; eine neue Vorlage erteilt keine zusätzlichen.

Der Bearbeiter liest den Projektadapter und die dort genannten Originalquellen. Er prüft Arbeitsverzeichnis, Branch und vorhandene Änderungen. Veraltete Dokumentation oder widersprüchliche Vorgaben werden am betroffenen Punkt benannt, nicht durch eine neue globale Regel verdeckt.

## 2. Den vorhandenen Ablauf nutzen

Nutze das bestehende Issue, den Plan oder den bereits zuständigen Patch-/Review-Ablauf. Ein kurzer Auftrag benötigt keine zusätzliche Projektverwaltung. Bei größerer Arbeit werden Abhängigkeiten und verantwortete Dateien so weit aufgeteilt, dass unabhängige Teile parallel bearbeitet werden können.

Vor einer fachlichen Entscheidung soll eine konkrete Option mit Folgen vorliegen. Für empirische Fragen untersucht der Bearbeiter zuerst Code, vorhandene Tests und verfügbare Belege. Unsichere Produktentscheidungen werden nicht stillschweigend getroffen.

## 3. Umsetzen und passend prüfen

Änderungen erhalten vorhandene Arbeit und beheben die Ursache im zuständigen Modul. Bei Fehlern wird nach Möglichkeit zuerst der fehlerhafte Ablauf nachgewiesen. Danach folgen die Prüfungen, die das neue Verhalten und die betroffenen Grenzen absichern; lokale Pflichtprüfungen bleiben verpflichtend.

Bei reinen Dokumentänderungen genügen die einschlägigen Inhalts-, Pfad- und Linkprüfungen, sofern das Projekt keine weitergehenden Vorgaben hat. Prüfung und Umgebung werden konkret genannt. „Nicht ausgeführt“ ist kein Erfolg. Ein lokaler grüner Test ist weder ein grüner Release-Commit noch ein Beleg für ein Gerät oder einen laufenden Server.

## 4. Den tatsächlichen Diff prüfen

Der Review zeigt die aktuellen Änderungen mit ihrer Begründung, relevante Prüfresultate und offene Grenzen. Der bestehende Reviewer beziehungsweise Hauptagent bewertet den vollständigen betroffenen Vertrag, einschließlich Dokumentation und notwendiger Plattformen. Ein Review erzeugt allein keine Commit-, Push- oder Deploymentbefugnis.

Für die Übernahme dieser Arbeitsregeln muss vorab sichtbar sein, welche Dateien neu hinzukommen und welcher lokale Einstieg ergänzt wird. Der gemeinsame Kern bleibt nachrangig. Konflikte mit vorhandenen Regeln werden mit Fundstelle vorgelegt. Nach dem Rootreview kann die bereits autorisierte Übernahme erfolgen; es gibt keine automatische Verteilung.

## 5. Ergebnis und offenen Rest übergeben

Die Übergabe nennt das erreichte Verhalten oder Dokument, den überprüften Stand, ausgeführte und ausgefallene Prüfungen sowie tatsächlich verbleibende Arbeit. Commit, CI, Deployment, Store-Verarbeitung und öffentliche Freigabe werden getrennt berichtet. Eine bereits vorhandene Übergabe wird aktualisiert, wenn der Auftrag das verlangt; eine neue Taskqueue entsteht dadurch nicht.

Regeländerungen werden in Git als eigener nachvollziehbarer Diff gepflegt. Das Zielprojekt übernimmt neue Fassungen bewusst. Lokale Ergänzungen und spätere Arbeit bleiben auch bei einer Rücknahme erhalten.
