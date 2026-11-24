# Changelog

## 1.5.0

Kamera flüssig: bis zu 30 Bilder/s.

- Die Kamera kommt jetzt als Video (H.264) statt als Einzelbilder. Das braucht etwa ein Zehntel der Daten und läuft auch über die Website flüssig.
- 30 statt 15 Bilder/s. Auflösung und Bilder/s stehen oben links.
- Ist die Verbindung zu langsam, werden Bilder übersprungen, statt immer weiter hinterherzuhängen.
- Alte Browser ohne Video-Decoder bekommen weiter Einzelbilder.
- Beim Installieren startet der Media-Dienst neu, das Radio setzt kurz aus.

## 1.4.1

Bessere Kamera-Qualität.

- Die Kamera startet jetzt in HD (1280×720) statt 640×480. Oben links steht die Auflösung.
- Für 50-Hz-Licht eingestellt: weniger Flimmern und Streifen.
- Neu: „Bild“ mit Reglern für Helligkeit, Kontrast, Sättigung, Schärfe, Gegenlicht und Zoom, je nach Kamera. Die Werte merkt sich der Browser pro Kamera.

## 1.4.0

Kurzbefehl-Uploads sind jetzt privat, und Updates klappen auch aus einem privaten Repository.

- Was per iPhone-Kurzbefehl hochgeladen wird, bekommt keinen Link mehr. Einen Link gibt es erst, wenn man ihn im File Host erstellt. Der Kurzbefehl bekommt nur noch den Dateinamen zurück. Links von früher bleiben, bis man sie entfernt.
- Updates: Unter „Privates Repository“ lässt sich ein GitHub-Schlüssel eintragen, der nur lesen darf. Er liegt nur für root lesbar auf dem Server.

## 1.3.0

Neu: Kamera-Tab (standardmäßig aus).

- Nach Kameras suchen (eingebaut oder per USB am Server), eine auswählen und live ansehen. Normal oder HD, mit Vollbild.
- Die Kamera ist nur an, solange jemand zuschaut. Nichts wird aufgenommen, kein Ton. Nur mit Admin-Login.
- Der Tab erscheint nur auf Servern, auf denen er eingeschaltet wird: `touch ~/panel-agent/camera.on`. Wieder aus: `rm ~/panel-agent/camera.on`. Danach den Admin neu laden.
- File Host: weitere englische Texte übersetzt.

## 1.2.2

Fix: Word-Dateien vom iPhone-Kurzbefehl wurden zu .txt.

- Der Server schaut jetzt zuerst in die Datei und erkennt Word, Excel, PowerPoint (auch alte Formate), OpenDocument, RTF, PDF, Bilder und Videos. Die Typangabe vom Handy zählt nur noch, wenn der Inhalt unklar ist.
- Falsch als .txt gespeicherte Kurzbefehl-Uploads werden beim Öffnen vom File Host umbenannt. Ihr Link ändert sich dadurch. Hat das iPhone nur reinen Text geschickt, ist das Original nicht dabei: dann neu hochladen.
- Namen wie „Bericht v1.2“ bekommen jetzt auch eine Endung.
- Echte .json-Dateien lassen sich wieder hochladen (ging seit 1.2.1 nicht), und bestimmte Textdateien werden nicht mehr für MP3 gehalten.

## 1.2.1

Fix für Uploads vom iPhone.

- Fotos und Videos vom iPhone-Kurzbefehl kamen ohne Dateiendung an (z. B. „IMG_1234“) und ließen sich weder ansehen noch richtig herunterladen. Jetzt erkennt der Server, was es wirklich ist, und hängt .jpg, .heic, .mov, .mp4 usw. an.
- Schon hochgeladene Dateien ohne Endung werden beim Öffnen vom File Host automatisch repariert.
- iPhone-Fotos im HEIC-Format bekommen jetzt eine Vorschau, auch am PC.
- Ist im Kurzbefehl beim Haupttext „Formular“ oder „JSON“ eingestellt statt „Datei“, kommt jetzt eine klare Fehlermeldung.

## 1.2.0

Dateien sind privat, Links gibt's nur auf Wunsch.

- Neue Dateien im File Host sind privat. Ohne Anmeldung kommt niemand dran.
- „Link erstellen“ macht eine Datei teilbar und kopiert den Link. „Nicht mehr teilen“ macht sie wieder privat, der Link geht dann nicht mehr.
- Geteilte Dateien sind in der Liste mit „geteilt“ markiert.
- Antippen öffnet die Datei direkt im File Explorer: Bilder, Videos, Musik, Textdateien sowie PowerPoint, Word, Excel und PDF. Mit den Pfeilen geht's zur nächsten Datei.
- Dateien von vor diesem Update bleiben geteilt, damit schon verschickte Links weiter funktionieren. Bei Bedarf einfach „Nicht mehr teilen“ drücken.
- Uploads per iPhone-Kurzbefehl bekommen weiterhin sofort einen Link.

## 1.1.2

Vorschau für PowerPoint, Word, Excel und PDF.

- Geteilte PowerPoint-Dateien (pptx, ppt, odp …) zeigen jetzt alle Folien als Bilder, direkt im Browser, auch am Handy.
- Genauso für Word (docx, doc, odt, rtf), Excel (xlsx, xls, ods) und PDF.
- Beim ersten Öffnen wird die Vorschau kurz erstellt (meist ein paar Sekunden). Danach ist sie sofort da.
- Gezeigt werden bis zu 40 Seiten.
- Hinweis: Dieses Update baut den Media-Dienst neu (LibreOffice kommt dazu). Das Installieren dauert deshalb einmalig etwa 5 Minuten und braucht rund 600 MB mehr Speicher.

## 1.1.1

Geteilte Dateien erst ansehen, dann herunterladen.

- Wer einen File-Host-Link im Browser öffnet (z. B. am Handy), sieht jetzt zuerst eine Vorschau statt eines sofortigen Downloads: Bilder, Videos, Musik und Textdateien direkt auf der Seite, alles andere mit Name und Größe.
- Darunter ein großer „Herunterladen“-Knopf. PDFs lassen sich zusätzlich im Browser öffnen.
- Gilt auch für Links, die schon verschickt wurden.
- Discord-Vorschauen und eingebettete Bilder funktionieren wie vorher.
- Die Seite ist auf Deutsch, wenn der Browser Deutsch eingestellt hat.

## 1.1.0

Admin-Panel jetzt auch auf Deutsch.

- Sprache umschaltbar: Deutsch oder Englisch, unter Design → Sprache oder direkt unter dem Login.
- Die Sprache wird pro Gerät gespeichert. Ohne Auswahl richtet sie sich nach der Sprache des Browsers.
- Datum, Uhrzeit und „vor 5 Minuten“ passen sich der Sprache an.
- Plugin-Beschreibungen von Modrinth bleiben im Original.

## 1.0.0

Erste Version auf GitHub.

- Updates per Knopfdruck: Admin-Panel → Updates. Mit Sicherung und automatischem Zurücksetzen.
- File Host als richtiger Explorer: Ordner, Unterordner, Verschieben (auch per Drag & Drop), Mehrfachauswahl, gut am Handy bedienbar.
- Ergebnisse von Converter, Downloader, Compressor, Background Remover und Upscaler landen automatisch in eigenen Ordnern im File Host.
- Speicherlimit einstellbar, mit automatischer Erkennung des Maximums.
- Upload vom iPhone/iPad per Kurzbefehl mit eigenem Upload-Schlüssel.
