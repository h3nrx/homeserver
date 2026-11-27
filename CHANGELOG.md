# Changelog

## 1.7.19

Cat and command line on the laptop screen.

- The cat looks like before again. It now sits at the right edge, blinks, flicks its tail, twitches an ear and takes a nap now and then ("z").
- New: a command line above the bottom line. Type / and a list of commands appears. Pick one with the arrow keys, Tab fills it in, Enter runs it.
- Commands: /minecraft start, stop and restart, /bots restart (all running Discord bots), /cat sleep and wake, /help. In German: /minecraft neustart, /bots neustart, /katze schlafen, /katze wach, /hilfe.
- It's not a Linux terminal: only these commands work. The normal login is still on Ctrl+Alt+F2.

## 1.7.18

Smaller, livelier cat.

- The cat on the laptop screen is now a small pixel cat: body, little ears, legs and one eye.
- The legs move with the steps, so it no longer slides across the ground. The tail swishes up and down.
- It hops now and then, sits down and blinks, and sometimes takes a nap with a little "z".

## 1.7.17

A cat on the laptop screen.

- At the bottom of the laptop screen, a little cat now strolls along a line.
- It walks left and right, hops now and then, sits down for a while, blinks and turns around.
- The values above stay where they are and still refresh every 2 seconds.

## 1.7.16

Laptop screen tidied up.

- The big numbers no longer have a percent sign. The % now sits small in the label below ("cpu %").
- The uptime moved from the bottom left to the middle, below the connection.

## 1.7.15

Calmer laptop screen.

- Four big numbers in the middle: CPU, temperature, RAM and disk. They turn yellow or red when things get tight.
- Below them one line of status dots: website, Discord bots, Minecraft and Ubuntu updates. Green means fine, yellow or red means something needs a look.
- The connection, name, time, date and uptime sit small and grey at the edges.
- One soft lilac accent on near-black instead of many colours.

## 1.7.14

Laptop screen in a new look.

- Rebuilt in the style of a Linux system info screen: the server name in big block letters, below it time and date on the left and the values on the right.
- New soft pastel colours on a dark background.
- Only what's useful: uptime, CPU with temperature, RAM, disk, services, Minecraft, updates and network.
- "Services" shows at a glance whether everything runs, or if the website is down or a Discord bot crashed.
- "Updates" shows how many Ubuntu updates are ready, how many are for security, and whether a restart is needed.

## 1.7.13

Everything in English.

- README, install and update scripts, the changelog and all release notes are now in English, so anyone can set up their own server.
- The install guide in the README now lists every question the installer asks, plus the optional extras (playit, wallet, camera).
- Release notes now come in English and German. The admin shows the language you picked.
- Error messages from updates still show up in German when the admin is set to German.

## 1.7.12

Grade calculator with school grades.

- The overall average and every subject now also show the grade from 1 to 6, with decimals.
- Conversion: grade = (17 − points) ÷ 3. So 15 points = 0.67, 14 points = 1.0, 13 points = 1.33 and so on.

## 1.7.11

Grade calculator.

- New panel "Grade calculator" for the Thuringian upper-school system (points 1–15).
- You add, rename and delete your subjects yourself. The list starts out empty.
- At the top, a big circle shows your overall average. It updates right away with every change.
- Click a subject to enter grades. Every grade gets a title, e.g. "Push-ups", and is either a normal grade or a course exam.
- Weighting: normal grades count 2/3, the course exam 1/3. The calculation is shown openly on the subject page.
- Grades can be tapped, changed and deleted. There's also a tile for it on the start page.

## 1.7.10

New designs.

- Six new designs: Contour, Graphite, Ember, Terminal, Deep sea and Dusk. Each has its own colours, its own font and a calm background, no more photos.
- Tiles are now clear and calm instead of milky and blurred.
- The Design page shows a live preview: point at a design to see it, click to use it.
- Accent colour, font and corners sit collapsed under "Fine-tune". The language stays visible at all times.
- Old designs are switched to the matching new one automatically.

## 1.7.9

Laptop screen in your language.

- The laptop screen shows everything in German or English, depending on the language last picked in the admin.
- Design: the Contour background now also stays saved when it's picked for another design.

## 1.7.8

Laptop screen in colour.

- Its own soft colours instead of the console's harsh default colours.
- Thin coloured bars for CPU, RAM and disk. They turn yellow and red when things get tight.
- Green, yellow and red dots for Minecraft, website and Ubuntu.
- New: current download and upload speed at the bottom.

## 1.7.7

New design and a status on the laptop screen.

- Admin: new design "Contour" in the look of the start page: black, clean, with slowly drifting lines. Turn it on under Design.
- Laptop screen: under System → Laptop screen you can turn on a status screen. Instead of the login, the laptop then shows the time, CPU, RAM, disk, Minecraft, website, Ubuntu updates and the connection. The login is still there with Ctrl+Alt+F2.

## 1.7.6

Ubuntu updates: phased updates.

- Updates that Ubuntu rolls out step by step and hasn't released for this laptop yet are no longer shown as installable. They come by themselves as soon as Ubuntu releases them.

## 1.7.5

Progress bar and faster updates.

- File Downloader: the progress bar now shows the real progress instead of jumping from 0 to 100 %. Video and audio count together as one bar.
- Updates only rebuild the media service when it's really needed. That saves several minutes and a lot of CPU.

## 1.7.4

Security update.

- The server keeps fewer logs, and existing logs of the website and the media service are cleared during the update.
- Deleted things are gone right away and completely.

## 1.7.3

Ubuntu updates in the admin.

- The Updates page shows whether there are updates for Ubuntu, how many of them are for security and whether a restart is needed.
- Install with a button. The laptop doesn't restart by itself.

## 1.7.2

More protection when signing in.

- After 3 wrong passwords the IP address is blocked for 24 hours. This also holds after a restart.
- Signing in shows how many tries are left.
- Blocked addresses are listed in the System overview and can be unblocked there.

## 1.7.1

Time-lapse in the Camera tab.

- Press record, press stop, and the video is done. Or set a duration first (1 hour to 7 days or your own time), then it stops by itself.
- Length of the finished video selectable: 15 seconds to 2 minutes.
- Needs little space: a picture is only taken every few seconds, and the single pictures are deleted as soon as the video is done.
- Keeps running on the server even when the page is closed, and carries on after a restart or update.
- Watch, download and delete videos right there. Only visible in the admin.

## 1.7.0

New public start page: contour lines instead of the laptop.

- Thin lines on black that drift very slowly and lift a little under the mouse or finger.
- Name at the bottom left, time at the bottom right. If someone plays Minecraft, that's shown small above it.
- New preview picture when you send the link.

## 1.6.0

New bento start page in the admin, and panels can finally be arranged properly.

- Start page: everything in one grid. Clock and load are one big tile, the rest is small or wide.
- Arranging rebuilt: the tile follows the mouse or finger, the others slide aside, and a dashed gap shows where it lands.
- On the phone: hold briefly to move, swipe to scroll. The handle works right away.
- New button for the size: small, wide or large.
- Every change is saved right away. Old arrangements and hidden panels are kept.
- Updates page: the version number no longer appears twice in the title.

## 1.5.4

A little UI cleanup.

- Start page: the labels now fit inside the rings (RAM, disk).

## 1.5.3

Updates page tidied up.

- One calm, centred page instead of three boxes: status, install, what's new.
- What's new is shown as a proper list.
- Source and access key are shown small below.

## 1.5.2

Camera tile on the start page.

- The camera now has its own tile with the number of cameras found. It can be moved and hidden like the others.
- On servers without the camera turned on, it stays invisible.

## 1.5.1

Camera: 30 fps even in low light.

- Some cameras halve their frame rate in low light because the auto exposure takes longer. This is now detected, and the exposure gets locked after a few seconds (the picture gets a little darker).
- Every 5 minutes the light is checked again briefly, which can stutter for 3–4 seconds.

## 1.5.0

Smooth camera: up to 30 fps.

- The camera now comes as video (H.264) instead of single pictures. That needs about a tenth of the data and also runs smoothly over the website.
- 30 instead of 15 fps. Resolution and fps are shown at the top left.
- If the connection is too slow, frames are skipped instead of falling further and further behind.
- Old browsers without a video decoder keep getting single pictures.
- Installing restarts the media service, so the radio pauses briefly.

## 1.4.1

Better camera quality.

- The camera now starts in HD (1280×720) instead of 640×480. The resolution is shown at the top left.
- Set for 50 Hz light: less flicker and banding.
- New: "Picture" with sliders for brightness, contrast, saturation, sharpness, backlight and zoom, depending on the camera. The browser remembers the values per camera.

## 1.4.0

Shortcut uploads are private now, and updates also work from a private repository.

- What you upload with the iPhone shortcut no longer gets a link. A link only exists once you create it in the File Host. The shortcut only gets the file name back. Links from before stay until you remove them.
- Updates: under "Private repository" you can enter a read-only GitHub key. It's stored on the server, readable by root only.

## 1.3.0

New: Camera tab (off by default).

- Search for cameras (built in or USB on the server), pick one and watch it live. Normal or HD, with full screen.
- The camera is only on while someone is watching. Nothing is recorded, no sound. Admin login only.
- The tab only shows up on servers where it's turned on: `touch ~/panel-agent/camera.on`. Off again: `rm ~/panel-agent/camera.on`. Then reload the admin.
- File Host: more English texts translated.

## 1.2.2

Fix: Word files from the iPhone shortcut became .txt.

- The server now looks into the file first and recognises Word, Excel, PowerPoint (old formats too), OpenDocument, RTF, PDF, pictures and videos. The type the phone sends only counts when the content is unclear.
- Shortcut uploads wrongly saved as .txt are renamed when the File Host opens. Their link changes because of that. If the iPhone only sent plain text, the original isn't included: upload it again then.
- Names like "Report v1.2" now get an extension too.
- Real .json files can be uploaded again (broken since 1.2.1), and certain text files are no longer mistaken for MP3.

## 1.2.1

Fix for uploads from the iPhone.

- Photos and videos from the iPhone shortcut arrived without a file extension (e.g. "IMG_1234") and could neither be viewed nor downloaded properly. Now the server recognises what they really are and adds .jpg, .heic, .mov, .mp4 etc.
- Already uploaded files without an extension are repaired automatically when the File Host opens.
- iPhone photos in HEIC format now get a preview, also on the PC.
- If the shortcut's request body is set to "Form" or "JSON" instead of "File", there's now a clear error message.

## 1.2.0

Files are private, links only when you want them.

- New files in the File Host are private. Nobody gets at them without signing in.
- "Create link" makes a file shareable and copies the link. "Stop sharing" makes it private again, and the link stops working.
- Shared files are marked "shared" in the list.
- Tapping opens the file right in the file explorer: pictures, videos, music, text files and PowerPoint, Word, Excel and PDF. The arrows go to the next file.
- Files from before this update stay shared so links you already sent keep working. If needed, just press "Stop sharing".
- Uploads with the iPhone shortcut still get a link right away.

## 1.1.2

Previews for PowerPoint, Word, Excel and PDF.

- Shared PowerPoint files (pptx, ppt, odp …) now show all slides as pictures, right in the browser, on the phone too.
- The same for Word (docx, doc, odt, rtf), Excel (xlsx, xls, ods) and PDF.
- The first time it opens, the preview is made briefly (usually a few seconds). After that it's there right away.
- Up to 40 pages are shown.
- Note: this update rebuilds the media service (LibreOffice is added). Installing therefore takes about 5 minutes once and needs around 600 MB more disk space.

## 1.1.1

View shared files first, then download.

- Opening a File Host link in the browser (e.g. on the phone) now shows a preview first instead of downloading right away: pictures, videos, music and text files right on the page, everything else with name and size.
- Below it, a big "Download" button. PDFs can also be opened in the browser.
- This also applies to links that were already sent.
- Discord previews and embedded pictures work like before.
- The page is in German if the browser is set to German.

## 1.1.0

Admin panel now in German too.

- Switchable language: German or English, under Design → Language or right below the login.
- The language is saved per device. Without a choice it follows the browser's language.
- Date, time and "5 minutes ago" adapt to the language.
- Plugin descriptions from Modrinth stay in their original language.

## 1.0.0

First version on GitHub.

- One-click updates: admin panel → Updates. With a backup and automatic rollback.
- File Host as a real explorer: folders, subfolders, moving (drag & drop too), multi-select, easy to use on the phone.
- Results of the converter, downloader, compressor, background remover and upscaler land automatically in their own folders in the File Host.
- Adjustable storage limit, with automatic detection of the maximum.
- Upload from iPhone/iPad with a shortcut and its own upload key.
