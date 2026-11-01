# Grumble

Talk out loud while you playtest your game. Grumble records the session, transcribes it on your
machine, and uses Claude to turn what you said into proposed bug reports and feature requests.

Filing approved issues to GitHub isn't built yet. For now, you read the proposals as JSON or CSV.

**Requires:** Windows, Python 3.12 or newer, and a microphone. An NVIDIA GPU is optional but gives
better transcripts. Writing proposals needs an [Anthropic API key](https://console.anthropic.com/settings/keys),
at roughly $0.07 for a 10-minute session.

## Install

With [pipx](https://pipx.pypa.io/):

```powershell
# NVIDIA GPU (downloads about 1.2 GB of CUDA libraries)
pipx install "grumble[cuda] @ git+https://github.com/jojojobring/grumble"

# No NVIDIA GPU
pipx install "git+https://github.com/jojojobring/grumble"
```

## Get started

```powershell
grumble setup                                  # connect your Claude key and GitHub, choose a mic
grumble init my-game --repo you/my-game        # create a project; --repo is optional
```

Your project config is in `%APPDATA%\Grumble\projects\my-game.yaml`. Add your game's terms to
`vocabulary`. To use a different mic for one project, set `audio.device` to part of its name.

## Use

```powershell
grumble start --project my-game   # press the hotkey (F9) when something matters
grumble stop                      # or Ctrl+C in the recording window
grumble process latest            # propose issues with Claude; writes proposals.json
grumble export latest             # write proposals.csv to read in Excel; edits aren't read back
grumble transcribe latest         # optional: redo the transcript; add --model to compare models
```

`start` transcribes after recording stops, so no speech model runs during play. Pass
`--no-transcribe` to do it later, for example if you're going straight back into a game.

The first transcription downloads a Whisper model from Hugging Face: `large-v3-turbo` (about 1.5 GB)
with a GPU, or `small.en` (about 500 MB) without one.

## Privacy

- Audio and transcription stay on your machine.
- `grumble process` sends the transcript, marker details, game state and marker screenshots to
  Anthropic.
- Screenshots capture your whole game monitor, so anything visible there can be included.
- Everything is stored in `%APPDATA%\Grumble`. Set `GRUMBLE_HOME` to use another folder.

## Session folder

Every stage can be re-run from these files.

| File | Contents |
|------|----------|
| `session.json` | Project, status, duration, mic, config snapshot |
| `audio/chunk_*.flac` | The whole session as 16 kHz mono audio |
| `markers.jsonl` | Each marker's time, window title and file paths |
| `screenshots/`, `gamestate/` | One file per marker |
| `segments.json` | Speech spans found by voice activity detection |
| `transcript.json`, `transcript.txt` | Timestamped text |
| `proposals.json`, `proposals.csv` | Proposed issues, uncertain items and discarded lines |

Earlier runs of `process` are kept as `proposals.<time>.json`.

## Good to know

- **Model:** `process` uses `claude-opus-5-5`. Set `GRUMBLE_MODEL` or pass `--model` to change it.
- **Screenshots:** use borderless windowed. Exclusive fullscreen can capture black, which is recorded
  as a warning on the marker.
- **Hotkey:** the game doesn't receive the key while Grumble runs. If Grumble exits, the key goes
  back to the game, so don't pick one that does something costly, such as Skyrim's F9 quickload. If
  a game runs as admin, Grumble must too or the hotkey won't fire.
- **Vocabulary:** Whisper uses about 224 tokens of it. Grumble warns above that limit.
- **Crashes:** run `grumble transcribe <session>` to recover a session after a crash or a closed
  window. Only the last moments before the crash are lost.

## Development

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[cuda,dev]"
.venv\Scripts\python -m pytest                                  # no hardware needed
.venv\Scripts\python scripts\manual_capture_test.py --project my-game --seconds 20 --transcribe
```

## License

MIT
