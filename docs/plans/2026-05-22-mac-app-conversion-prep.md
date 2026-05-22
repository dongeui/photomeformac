# Photome Mac App Conversion Preparation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Convert the current Photome backend/web/Docker workspace into a Mac-first app workspace where users install a macOS app and do not need Docker.

**Architecture:** Keep Photome core behavior intact: FastAPI + scanner + SQLite + web UI remains the runtime. Add a macOS shell that starts/stops the backend as a local process, manages app data/model cache/folder permissions, and opens the existing dashboard/gallery. Docker stays as a server/developer/Windows path, not as the normal Mac user path.

**Tech Stack:** Python/FastAPI/SQLite existing core, macOS app shell to be selected between Swift/SwiftUI, Tauri, or Electron; local process supervisor; WebView or browser launcher; macOS app data directories; future signing/notarization/DMG/updater.

---

## Non-goals for this phase

1. Do not rewrite the backend.
2. Do not remove Docker support.
3. Do not implement public internet exposure.
4. Do not change source media identity semantics.
5. Do not package large CLIP model weights into the first app build unless explicitly decided later.
6. Do not reset or re-key existing person/alias/merge/photo state.

## Product decisions already made

1. Mac app is the primary distribution.
2. Docker-level features should be integrated into the Mac app so normal Mac users do not install Docker.
3. Official network scope is local-only plus explicit LAN sharing.
4. Windows is future Docker-first, not current native app work.
5. Current repo `/Users/dongeui/Desktop/code/photomeformac` is the Mac app workspace.
6. Existing Photome invariants still apply: source roots are read-only, `file_id` is identity, and people/person state is cumulative across path changes.

## Recommended app-shell choice

Start with Swift/SwiftUI unless a quick prototype proves Tauri is dramatically faster.

Reason:
1. Best macOS folder permission UX.
2. Best menu bar/status/launch-at-login integration.
3. Best signing/notarization path.
4. Avoids shipping a large Electron runtime.
5. The UI can still reuse the existing web dashboard through WebView or external browser.

Fallback:
- Tauri is acceptable if we want faster web-wrapper iteration with smaller footprint than Electron.
- Electron should be last choice because Photome already has heavy AI/runtime concerns; adding Electron size is unattractive.

---

## Phase 0: Repository hygiene and baseline

### Task 0.1: Confirm copied workspace is clean

**Objective:** Ensure the new repo contains source/docs/tests only, not local DB/model/runtime artifacts.

**Files:**
- Inspect: `.gitignore`
- Inspect: `data/`
- Inspect: `logs/`

**Commands:**

```bash
cd /Users/dongeui/Desktop/code/photomeformac
git status --short
find data -maxdepth 3 -type f | sort
find . -maxdepth 2 \( -name '*.sqlite*' -o -name '*.db*' -o -name '.env' -o -name 'model_cache' -o -name 'derived_root' \) -print
```

**Expected:**
- Clean git status.
- Only safe checked-in files under `data/`, such as `.gitkeep` and helper scripts.
- No private DB/model/photo artifacts.

### Task 0.2: Update repo identity docs

**Objective:** Make this repo self-describe as the Mac app workspace, not merely a copy of core Photome.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `AGENTS.md`

**Content to add:**
- This repo is `photomeformac`.
- Goal is Mac app integration around Photome core.
- Docker remains supported for server/dev/Windows path.
- Mac app work must preserve all Photome invariants.

**Verification:**

```bash
git diff -- README.md docs/README.md AGENTS.md
```

**Commit:**

```bash
git add README.md docs/README.md AGENTS.md
git commit -m "docs: define Mac app workspace identity"
```

---

## Phase 1: Runtime boundary definition

### Task 1.1: Document backend runtime contract

**Objective:** Define what the Mac shell needs from the Python backend.

**Files:**
- Create: `docs/mac/RUNTIME_CONTRACT.md`

**Must specify:**
1. Start command.
2. Required environment variables.
3. Default bind mode: `127.0.0.1`.
4. LAN bind mode: explicit user option only.
5. Data paths:
   - config
   - SQLite DB
   - derived assets
   - model cache
   - source roots
6. Health endpoint.
7. Shutdown behavior.
8. Log location.
9. Offline AI/model cache behavior.

**Verification:**

```bash
sed -n '1,240p' docs/mac/RUNTIME_CONTRACT.md
```

### Task 1.2: Add a Mac runtime env builder script

**Objective:** Create a deterministic script that prints/launches the backend with Mac-app-style paths.

**Files:**
- Create: `scripts/mac_app_backend_env.py`
- Test: `tests/test_mac_app_backend_env.py`

**Behavior:**
- Given an app data root, produce env vars:
  - `PHOTOME_SERVER_HOST=127.0.0.1`
  - `PHOTOME_SERVER_PORT=0` or configured port strategy, pending implementation choice
  - `PHOTOME_DATA_ROOT=<app_data>/data`
  - `PHOTOME_DERIVED_ROOT=<app_data>/derived`
  - `PHOTOME_MODEL_ROOT=<app_data>/models`
  - `PHOTOME_DATABASE_PATH=<app_data>/data/photome.sqlite3`
  - `PHOTOME_OFFLINE_MODE=1`
- Should not require Docker.

**Test expectations:**
- Paths are under the provided app data root.
- Source roots are not rewritten as Docker `/photos` paths.
- Offline mode defaults to enabled.

**Commands:**

```bash
python -m pytest tests/test_mac_app_backend_env.py -q
```

### Task 1.3: Add backend launch probe for Mac mode

**Objective:** Prove the backend can start as a local process without Docker using Mac-app-style env.

**Files:**
- Create: `scripts/mac_app_smoke.py`
- Optional test: `tests/test_mac_app_smoke.py` if reliable without long startup cost

**Behavior:**
1. Create a temp app data dir.
2. Launch backend on localhost.
3. Poll `/status` or `/healthz`.
4. Print selected port and status summary.
5. Terminate backend cleanly.

**Verification:**

```bash
python scripts/mac_app_smoke.py
```

Expected:
- Backend starts.
- Health probe passes.
- Process exits cleanly.

---

## Phase 2: macOS shell MVP decision

### Task 2.1: Create shell technology decision record

**Objective:** Decide SwiftUI vs Tauri vs Electron based on Photome needs.

**Files:**
- Create: `docs/mac/APP_SHELL_DECISION.md`

**Evaluation matrix:**
1. Folder permission UX.
2. Process supervision.
3. WebView/browser support.
4. Menu bar/settings UX.
5. Build size.
6. Signing/notarization/updater complexity.
7. Developer speed.
8. Compatibility with Python backend bundling.

**Recommended default:** SwiftUI first.

### Task 2.2: Create minimal app skeleton

**Objective:** Add the chosen macOS shell skeleton without yet packaging Python.

**Files if SwiftUI:**
- Create: `mac/PhotomeForMac/PhotomeForMac.xcodeproj` or Swift Package/Xcode project layout
- Create: `mac/PhotomeForMac/PhotomeForMac/App.swift`
- Create: `mac/PhotomeForMac/PhotomeForMac/ContentView.swift`
- Create: `mac/PhotomeForMac/README.md`

**MVP UI:**
- App title.
- Backend status: stopped/running/error.
- Button: Start backend.
- Button: Open dashboard.
- Button: Stop backend.

**Verification:**
- Open/build in Xcode or run xcodebuild if project format supports it.

---

## Phase 3: Backend process supervisor

### Task 3.1: Define process supervisor API

**Objective:** Specify how the app starts, monitors, and stops the backend.

**Files:**
- Create: `docs/mac/BACKEND_SUPERVISOR.md`

**Must cover:**
1. Spawn command.
2. Environment injection.
3. stdout/stderr log routing.
4. Health polling.
5. Port selection.
6. Crash handling.
7. Shutdown on app exit.
8. No destructive cleanup of DB/derived/model cache.

### Task 3.2: Implement supervisor in app skeleton

**Objective:** Start the backend from the app shell during development.

**Files if SwiftUI:**
- Create: `mac/PhotomeForMac/PhotomeForMac/BackendSupervisor.swift`
- Modify: `mac/PhotomeForMac/PhotomeForMac/ContentView.swift`

**Development-mode command:**
- Use local repo Python command first.
- Do not attempt full bundled runtime yet.

**Verification:**
1. Start app.
2. Click start backend.
3. Health becomes running.
4. Open dashboard works.
5. Stop backend kills only the child process.

---

## Phase 4: App data and source-folder UX

### Task 4.1: Define app data layout

**Objective:** Choose stable macOS directories.

**Files:**
- Create: `docs/mac/APP_DATA_LAYOUT.md`

**Proposed layout:**
- `~/Library/Application Support/Photome/config.json`
- `~/Library/Application Support/Photome/data/photome.sqlite3`
- `~/Library/Application Support/Photome/derived/`
- `~/Library/Application Support/Photome/models/`
- `~/Library/Logs/Photome/`

**Important:**
- Source photos remain wherever the user chose them.
- Source roots are read-only.
- Path changes must not reset identity/person data.

### Task 4.2: Implement folder selection contract

**Objective:** Define how selected folders are stored and passed to backend.

**Files:**
- Create: `docs/mac/FOLDER_ACCESS.md`
- Later app files depending on shell choice

**Must cover:**
1. NSOpenPanel folder selection.
2. Security-scoped bookmarks if sandboxing is used.
3. Multiple source roots.
4. NAS/Volumes offline handling.
5. Read-only expectation.

---

## Phase 5: LAN sharing mode

### Task 5.1: Define LAN sharing behavior

**Objective:** Make local-only the default and LAN sharing explicit.

**Files:**
- Create: `docs/mac/LAN_SHARING.md`

**Rules:**
1. Default bind: `127.0.0.1`.
2. LAN mode bind: `0.0.0.0` or selected interface.
3. UI must show the LAN URL.
4. UI must warn that same-network devices can access photos.
5. Public internet is not supported.
6. Future auth/admin separation remains a hardening item.

### Task 5.2: Add backend env switch for LAN mode

**Objective:** Make the Mac runtime env builder support local vs LAN mode.

**Files:**
- Modify: `scripts/mac_app_backend_env.py`
- Modify: `tests/test_mac_app_backend_env.py`

**Tests:**
- Local mode uses `127.0.0.1`.
- LAN mode uses `0.0.0.0` or selected interface.
- LAN mode is never default.

---

## Phase 6: Model manager

### Task 6.1: Define AI model cache UX

**Objective:** Keep the base app install smaller while supporting CLIP.

**Files:**
- Create: `docs/mac/MODEL_MANAGER.md`

**Rules:**
1. App can run without CLIP model installed.
2. Model download is user-initiated or clearly explained.
3. Offline mode uses existing cache only.
4. Base runtime import must not fail if optional AI deps/model are missing.
5. Dashboard must explain why Image AI counts differ from file counts.

### Task 6.2: Map existing `/ai-pack` endpoints to app UI

**Objective:** Define which backend endpoints the Mac app will call for model status/prepare.

**Files:**
- Modify: `docs/mac/MODEL_MANAGER.md`

**Include:**
- Status check endpoint.
- Prepare/download endpoint.
- Offline/cache-only behavior.
- Failure messages.

---

## Phase 7: Packaging path

### Task 7.1: Define bundling strategy for Python runtime

**Objective:** Decide how Python backend gets into `.app`.

**Files:**
- Create: `docs/mac/PYTHON_BUNDLING.md`

**Candidates:**
1. Embedded Python distribution.
2. PyInstaller/Nuitka-style backend binary.
3. uv/venv materialized into app resources.
4. Release artifact built from core repo.

**Evaluation:**
- Size.
- Native libs: Pillow, pillow-heif, torch, open_clip, onnx/opencv.
- Codesigning nested binaries.
- Startup speed.
- Update strategy.

### Task 7.2: Define release artifact strategy

**Objective:** Separate app shell release from backend/core release cleanly.

**Files:**
- Create: `docs/mac/RELEASE_STRATEGY.md`

**Include:**
1. `.app` build.
2. `.dmg` packaging.
3. Signing.
4. Notarization.
5. Auto-update candidate.
6. How to update backend code inside app.
7. How Docker/server release remains separate.

---

## Phase 8: Validation and acceptance

### Task 8.1: Define MVP acceptance checklist

**Objective:** Make “Mac conversion MVP” testable.

**Files:**
- Create: `docs/mac/MVP_ACCEPTANCE.md`

**Acceptance criteria:**
1. User launches app with no Docker installed.
2. App starts backend locally.
3. Dashboard opens.
4. User selects photo folder.
5. Scan runs without writing to source folder.
6. Gallery displays photos.
7. App data is stored under macOS app support path.
8. App restarts and preserves DB/person state.
9. LAN sharing can be enabled explicitly.
10. Image AI state is visible and explained even if model is missing.

### Task 8.2: Add smoke test checklist script

**Objective:** Provide a repeatable local validation script.

**Files:**
- Create: `scripts/mac_app_validate.sh`

**Checks:**
1. Repo clean/expected.
2. Backend smoke starts.
3. Health passes.
4. No Docker required.
5. No DB/model artifacts accidentally staged.

---

## First execution recommendation

Start with these four tasks only:

1. Task 0.1: repo hygiene check.
2. Task 0.2: repo identity docs.
3. Task 1.1: runtime contract.
4. Task 1.2: Mac runtime env builder + tests.

Why:
- They clarify the conversion boundary before choosing app tech.
- They reduce risk of building a shell that cannot reliably start Photome.
- They preserve the existing backend/Docker functionality while making Mac app work concrete.

## Validation commands after first batch

```bash
cd /Users/dongeui/Desktop/code/photomeformac
git status --short
python -m pytest tests/test_mac_app_backend_env.py -q
git log --oneline -n 5
```

## Expected first-batch output

1. `docs/mac/RUNTIME_CONTRACT.md`
2. `scripts/mac_app_backend_env.py`
3. `tests/test_mac_app_backend_env.py`
4. Updated repo identity docs
5. Passing env-builder tests
6. Commit pushed to `https://github.com/dongeui/photomeformac.git`
