import argparse
import dataclasses
import sys

from dotenv import load_dotenv
from jsonschema import ValidationError

from grumble.config import ConfigError, create_project, home, load_project
from grumble.session import ActiveLock, Session, SessionError, resolve_session


def _transcribe(session: Session, project: str, model: str | None = None) -> None:
    from grumble.transcribe import transcribe_session

    config = load_project(project)
    if model:
        config = dataclasses.replace(config, transcribe=dataclasses.replace(config.transcribe, model=model))
    result = transcribe_session(session, config)
    print(
        f"{len(result['segments'])} transcript line(s) in {result['transcribe_seconds']:.0f}s\n"
        f"Read it: {session.path / 'transcript.txt'}"
    )


def cmd_start(args: argparse.Namespace) -> None:
    from grumble.capture import run_capture

    result = run_capture(load_project(args.project))
    print(f"Saved {result.duration_seconds:.0f}s of audio and {result.markers} marker(s) to {result.session.path}")
    if args.no_transcribe:
        print(f"Transcribe later with: grumble transcribe {result.session.id}")
    else:
        _transcribe(result.session, args.project)


def cmd_stop(_args: argparse.Namespace) -> None:
    session = ActiveLock().request_stop()
    print(f"Stopping session {session}. Its window will finish up and transcribe.")


def cmd_transcribe(args: argparse.Namespace) -> None:
    session = resolve_session(args.session)
    _transcribe(session, args.project or session.read_manifest()["project"], args.model)


def cmd_process(args: argparse.Namespace) -> None:
    from grumble.synthesize import process_session

    session = resolve_session(args.session)
    result = process_session(session, load_project(args.project or session.read_manifest()["project"]), args.model)
    print(
        f"{len(result['new_issues'])} proposed issue(s), {len(result['uncertain'])} uncertain, "
        f"{result['discarded']['count']} line(s) discarded ({result['model']})"
    )
    for issue in result["new_issues"]:
        print(f"  [{issue['type']}/{issue['severity']}] {issue['title']}")
    for item in result["uncertain"]:
        print(f"  [uncertain] {item['summary']}")
    for warning in result["warnings"]:
        print(f"Warning: {warning}")
    print(f"Read it: {session.path / 'proposals.json'}")


def cmd_export(args: argparse.Namespace) -> None:
    from grumble.export import export_csv

    path, count = export_csv(resolve_session(args.session))
    print(f"Wrote {count} row(s) to {path}")


def cmd_init(args: argparse.Namespace) -> None:
    path = create_project(args.name, args.repo)
    print(f"Created {path}\nAdd your game's terms to it, then run: grumble start --project {args.name}")


def cmd_setup(args: argparse.Namespace) -> None:
    from grumble.setup_ui import run_setup

    run_setup(open_browser=not args.no_browser, port=args.port)


def cmd_devices(_args: argparse.Namespace) -> None:
    from grumble.recorder import list_input_devices

    for name, is_default in list_input_devices():
        print(f"{'* ' if is_default else '  '}{name}")
    print("\n* is the Windows default. Set audio.device to any part of a name.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grumble", description="Turn playtest commentary into issue reports")
    commands = parser.add_subparsers(required=True)

    start = commands.add_parser("start", help="record a session, then transcribe it")
    start.add_argument("--project", required=True)
    start.add_argument("--no-transcribe", action="store_true", help="skip transcription after recording")
    start.set_defaults(func=cmd_start)

    stop = commands.add_parser("stop", help="stop the recording session")
    stop.set_defaults(func=cmd_stop)

    transcribe = commands.add_parser("transcribe", help="re-run VAD and transcription from saved audio")
    transcribe.add_argument("session", nargs="?", default="latest", help="session folder name (default: latest)")
    transcribe.add_argument("--project", help="use this project's config instead of the session's")
    transcribe.add_argument("--model", help="override the Whisper model")
    transcribe.set_defaults(func=cmd_transcribe)

    process = commands.add_parser("process", help="turn a transcript into proposed issues with the Claude API")
    process.add_argument("session", nargs="?", default="latest", help="session folder name (default: latest)")
    process.add_argument("--project", help="use this project's config instead of the session's")
    process.add_argument("--model", help="override the Claude model")
    process.set_defaults(func=cmd_process)

    export = commands.add_parser("export", help="write proposals to a CSV for reading in a spreadsheet")
    export.add_argument("session", nargs="?", default="latest", help="session folder name (default: latest)")
    export.set_defaults(func=cmd_export)

    init = commands.add_parser("init", help="create a project config")
    init.add_argument("name")
    init.add_argument("--repo", help="GitHub repo for issues, as owner/name")
    init.set_defaults(func=cmd_init)

    setup = commands.add_parser("setup", help="connect your Claude API key and GitHub in a local page")
    setup.add_argument("--no-browser", action="store_true", help="print the address instead of opening it")
    setup.add_argument("--port", type=int, default=0, help="port to use (default: any free port)")
    setup.set_defaults(func=cmd_setup)

    devices = commands.add_parser("devices", help="list microphones")
    devices.set_defaults(func=cmd_devices)
    return parser


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Grumble only runs on Windows for now.")
    load_dotenv(home() / ".env")  # real environment variables take priority
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (ConfigError, SessionError, ValidationError, ValueError, RuntimeError) as error:
        sys.exit(f"Error: {error}")
    except Exception as error:
        from grumble.synthesize import SynthesisError

        if isinstance(error, SynthesisError):
            sys.exit(f"Error: {error}")
        raise
    except KeyboardInterrupt:
        sys.exit("Interrupted. Saved audio is safe; re-run with `grumble transcribe`.")


if __name__ == "__main__":
    main()
