# CFTP browser workspace

A local, responsive interface for the existing CFTP protocol. It provides file
search and sorting, drag-and-drop or multiple-file uploads, verified downloads,
checksum details, connection settings, and transfer history for the running session.
The refined interface follows MongoDB Atlas navigation patterns while retaining
CFTP's identity. Its Broadcasts page shows online clients, received files, and
per-recipient delivery results.

## Start and try it

From the project folder:

```powershell
python -m cftp.web --demo
```

Open **http://127.0.0.1:8787**. This command starts the browser interface and a
real CFTP TCP server on port 9000. The workspace connects automatically. Files
are saved in `server_storage/`, which is excluded from Git.

1. Click **Upload files**, or drag a file into the workspace.
2. Find it in the file list. Click its name to inspect its SHA-256 checksum.
3. Click its download icon and compare the downloaded file with the original.
4. Open **Transfer activity** to see completed transfers and integrity results.
5. Test an empty file, a binary file, a filename with spaces, or several files together.

To test broadcasts, start a separate receiving client:

```powershell
python -m cftp.client --download-dir downloads/client-2 listen
```

Then use a file's broadcast icon. Both this client and the workspace receive and
verify the file. Browser tabs sharing the same web bridge represent one receiving
client. See [Broadcast guide](broadcasts.md) for CLI commands and protocol details.

Uploads with the same name ask before replacing the existing file. The maximum
upload size is 100 MB per file. Uploaded files stay on disk after restarting;
transfer history lasts for the web server session. A connection failure produces
an error and offers a reconnect through the connection button.

Stop a foreground server with **Ctrl+C**.

PowerShell shortcut:

```powershell
.\scripts\run_web.ps1
# Choose different ports if either is already in use:
.\scripts\run_web.ps1 -Port 8788 -CftpPort 9002
# Keep it running in the background on Windows:
.\scripts\run_web.ps1 -Background
# Stop that background workspace:
.\scripts\stop_web.ps1
```

## Use an existing CFTP server

```powershell
python -m cftp.server --port 9000
# In a second terminal:
python -m cftp.web
```

Open the web interface and choose **Connect a server**. Enter the TCP server's
address and port. The web service binds to loopback; the CFTP destination may be
a reachable local or remote server. Browsers cannot speak raw TCP, so the web
service performs CFTP operations on their behalf.

## Run the tests

Python tests require no third-party packages:

```powershell
python -m unittest discover -s tests -v
```

Browser tests use Playwright as a development-only dependency:

```powershell
npm install
npx playwright install chromium
npm run test:ui
# To watch the browser:
npm run test:ui:headed
```

The browser suite starts its own isolated web and TCP servers on ports 8791 and
9001. It covers upload/download byte equality, checksum inspection, searching,
multiple files, empty files, mobile layout, disconnect/reconnect errors, and
drag-and-drop, and verified broadcast delivery to a separate client process.
Screenshots and failure traces go in the ignored `test-results/`
folder. Its temporary server files are under `.web-run/`.

If using an already installed Chromium binary, set `PLAYWRIGHT_CHROMIUM_PATH`
to its full executable path before running the suite.

## Implementation

- `cftp/web.py`: standard-library HTTP bridge; streams request bodies to isolated
  temporary files, then performs actual `CFTPClient` transfers over TCP.
- `cftp/static/`: buildless HTML, CSS (including the Atlas-inspired theme), JavaScript, and a vector favicon.
- `cftp/broadcast.py`: bounded fan-out, immutable snapshots, and verified recipient results.
- `tests/test_web.py`: HTTP-to-TCP integration and local-origin validation.
- `tests/browser/workspace.spec.cjs`: browser workflow and layout checks.

No npm installation or frontend build is needed to run the application. The
server verifies downloads before sending them to the browser. Failed uploads
and downloads never receive a successful integrity status. Web mutation requests
require the local request header; foreign origins and hosts are rejected.

This interface inherits CFTP's plain TCP transport and lack of peer authentication.
It is intended as a local workspace and test interface, not an internet-facing service.
