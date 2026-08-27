# TestFlight- und App-Store-Veröffentlichung ohne eigenen Mac

Das Repository ist vollständig für Codemagic vorbereitet. Der Bundle Identifier lautet:

```text
de.oliverzimmermann.rclonesync
```

## Einmalige Einrichtung

1. Dem [Apple Developer Program](https://developer.apple.com/programs/) beitreten. Eine kostenlose Apple-ID reicht für signierte TestFlight-Builds nicht aus.
2. In [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources/identifiers/list) eine explizite App-ID mit dem Bundle Identifier `de.oliverzimmermann.rclonesync` anlegen.
3. In [App Store Connect](https://appstoreconnect.apple.com/) unter **Apps → + → Neue App** einen App-Datensatz anlegen:
   - Plattform: iOS
   - Name: `Rclone Sync` oder ein noch verfügbarer Name
   - Bundle-ID: `de.oliverzimmermann.rclonesync`
   - SKU: `rclone-sync-ios`
4. Unter **Benutzer und Zugriff → Integrationen → App Store Connect API** einen Team-Schlüssel mit Rolle **App Manager** erzeugen. Die `.p8`-Datei sofort sicher speichern; Apple bietet sie nur einmal zum Download an.
5. Bei [Codemagic](https://codemagic.io/) mit GitHub anmelden und dieses Repository hinzufügen.
6. In den Codemagic-Team-Einstellungen unter **Developer Portal / App Store Connect** den Schlüssel aus Schritt 4 als Integration mit exakt diesem Namen hinterlegen:

   ```text
   rclone-sync-testflight
   ```

   Benötigt werden Issuer ID, Key ID und die `.p8`-Datei. Codemagic erzeugt beziehungsweise lädt damit Distribution-Zertifikat und Provisioning Profile automatisch.

## Erster Build

Nach der Einrichtung kann der Workflow **Rclone Sync · TestFlight** in Codemagic durch einen Release-Tag gestartet werden. Der Tag muss annotiert und kryptografisch signiert sein und exakt auf einem Commit liegen, der bereits in `origin/main` enthalten ist:

```bash
git switch main
git pull --ff-only origin main
git tag -s ios-v1.0.0 -m "Rclone Sync iOS 1.0.0"
git tag -v ios-v1.0.0
git push origin ios-v1.0.0
```

Der öffentliche Signierschlüssel muss im Codemagic-Buildschlüsselbund verfügbar sein. Die Pipeline holt `origin/main` und den exakten Tag erneut und bricht vor Versionsänderung, Build oder Publishing ab, wenn der Tag nur lightweight, nicht kryptografisch prüfbar, nicht auf dem Checkout oder nicht in der Main-Historie ist.

Die Pipeline:

1. verwendet die aktuelle stabile Xcode-26-Version,
2. generiert `RcloneMobile.xcodeproj` mit XcodeGen,
3. installiert das App-Store-Profil,
4. setzt eine eindeutige Buildnummer,
5. führt die iOS-Unit-Tests auf einem iPhone-17-Simulator aus,
6. erstellt die signierte IPA und
7. erzeugt echte, lokalisierte App-Store-Screenshots aus dem nativen Simulator,
8. baut eine öffentlich verteilbare IPA und lädt sie zu App Store Connect/TestFlight hoch.

Nach Apples Verarbeitung erscheint der Build unter **TestFlight**. Für interne Tests eine interne Testergruppe anlegen und den Build hinzufügen. Externe Tester benötigen beim ersten Build eine TestFlight-Betaprüfung.

Der Export ist nicht auf interne TestFlight-Gruppen beschränkt. Derselbe verarbeitete Build kann deshalb auch unter **App Store → iOS-App** ausgewählt und nach Pflege der Metadaten zur öffentlichen Prüfung eingereicht werden. Die Veröffentlichung bleibt in App Store Connect bewusst auf **manuell**, damit ein genehmigter Build nicht ungeplant live geht.

Für Apples Prüfung steht auf der Anmeldeseite eine vollständig lokale Vorschau mit Beispieldaten bereit. Sie benötigt weder private Serverzugänge noch eine Netzwerkverbindung. Die vorbereiteten Texte, URLs und Prüferhinweise stehen in [`APP_STORE_METADATA.md`](APP_STORE_METADATA.md).

## Sicherheitsregeln

- API-Schlüssel, `.p8`, Zertifikate und Provisioning Profiles niemals committen.
- Die App speichert das Serverpasswort nicht.
- `ITSAppUsesNonExemptEncryption` ist `false`, weil die App ausschließlich Apples integrierte Standard-TLS-Implementierung verwendet und keine eigene oder nicht freigestellte Kryptografie enthält.
- Produktive Server sollten per HTTPS erreichbar sein. Die App deaktiviert App Transport Security nicht global.
