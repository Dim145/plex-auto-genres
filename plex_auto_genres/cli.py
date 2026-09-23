"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from .config import (
    AppConfig,
    CONFIG_VERSION,
    CachedConfig,
    LibraryRun,
    config_json_schema,
    load_config,
    migrate_v1,
)
from .doctor import DoctorReport, run_doctor
from .errors import ConfigError, PagError, PlexConnectionError
from .migration import legacy_logs_dir, migrate_install
from .models import (
    KNOWN_GUID_SCHEMES,
    ExternalId,
    ManualTags,
    MediaType,
    RunReport,
    check_decision,
    clean_names,
)
from .plexsvc import client as plex_client
from .plexsvc.writer import undo_run
from .providers import LookupRequest, bindable_schemes, build_providers
from .reporting import ProgressBar, Style, print_report
from .runner import ACTIONS, run_libraries
from .scheduler import SchedulePlan, Scheduler, validate_cron
from .store import Store, run_status

log = logging.getLogger("plex_auto_genres")

DEFAULT_CONFIG = "config/config.json"
DEFAULT_DB = "logs/plex-auto-genres.db"


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


#: Id schemes a binding may name. Which of them a given library can actually
#: resolve is checked against its sources when the binding is set.
BINDABLE_SCHEMES = ["tmdb", "mal", "anilist", "anidb", "tvdb", "imdb"]


def _add_binding_parsers(sub) -> None:
    """The three commands over manual bindings, which an item holds per source."""
    bind = sub.add_parser("bind", help="Pin a Plex item to a specific provider id.")
    bind.add_argument("library")
    bind.add_argument("title", help="Plex title, or the cache key shown by 'failures'.")
    bind.add_argument("provider", choices=BINDABLE_SCHEMES)
    bind.add_argument("provider_id")
    bind.add_argument("--note", help="Free-text reminder of why this binding exists.")

    unbind = sub.add_parser("unbind", help="Remove an item's manual bindings.")
    unbind.add_argument("library")
    unbind.add_argument("title")
    unbind.add_argument("--provider", choices=BINDABLE_SCHEMES,
                        help="Remove just this source's id, leaving the others.")

    bindings = sub.add_parser("bindings", help="List manual bindings.")
    bindings.add_argument("--library")


def _add_manual_parsers(sub) -> None:
    """Genres decided by hand, which outrank the sources until handed back."""
    manual = sub.add_parser(
        "manual", help="Decide an item's genres (or collections) by hand, replacing any "
                       "earlier decision on it.")
    manual.add_argument("library")
    manual.add_argument("title", help="Title as Plex shows it (add the year if two share it), "
                                      "or the item's key, e.g. tmdb://1234.")
    manual.add_argument("--add", action="append", default=[], metavar="GENRE",
                        help="Always write this one. Repeat for several.")
    manual.add_argument("--remove", action="append", default=[], metavar="GENRE",
                        help="Never write this one, whatever the sources say.")
    manual.add_argument("--lock", action="store_true",
                        help="Stop asking the sources: genres become exactly --add; "
                             "collections get --add and lose --remove.")
    manual.add_argument("--note", help="Why, for whoever reads the list later. Kept from "
                                       "the earlier decision unless given; \"\" clears it.")

    unmanual = sub.add_parser("unmanual", help="Hand an item's genres back to the sources.")
    unmanual.add_argument("library")
    unmanual.add_argument("title")

    manuals = sub.add_parser("manuals", help="List the items whose genres are decided by hand.")
    manuals.add_argument("--library")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="plex-auto-genres",
        description="Tag your Plex media with genres from TMDB, MyAnimeList or AniList.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  plex-auto-genres run                       # every enabled library in the config\n"
            "  plex-auto-genres run --library Animes --dry # preview one library\n"
            "  plex-auto-genres query 'Cowboy Bebop' --type anime\n"
            "  plex-auto-genres bind Animes 'Monster' mal 19\n"
            "  plex-auto-genres undo 4f2a1c9b0e77\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config.json.")
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to the state database.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")

    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Process libraries (the default command).")
    run.add_argument("--library", action="append", help="Only this library. Repeatable.")
    run.add_argument("--type", choices=[t.value for t in MediaType],
                     help="Override the library type (requires a single --library).")
    run.add_argument("--dry", "--dry-run", dest="dry", action="store_true",
                     help="Report what would change without touching Plex.")
    run.add_argument("-f", "--force", action="store_true",
                     help="Ignore the cache and reprocess everything.")
    run.add_argument("-y", "--yes", action="store_true", help="Do not prompt for confirmation.")
    run.add_argument("--no-progress", action="store_true", help="Disable the progress bar.")
    run.add_argument("--only", choices=list(ACTIONS), action="append",
                     help="Run only these actions. Repeatable.")
    run.add_argument("--posters-dir", default="posters", help="Root of the poster directories.")

    query = sub.add_parser("query", help="Look up a title without changing anything.")
    query.add_argument("title", nargs="+")
    query.add_argument("--type", required=True, choices=[t.value for t in MediaType])
    query.add_argument("--year", type=int)
    query.add_argument("--provider", action="append", help="Override the provider order.")
    query.add_argument("--keywords", action="store_true", help="TMDB: fetch keywords, not genres.")

    _add_binding_parsers(sub)
    _add_manual_parsers(sub)

    undo = sub.add_parser("undo", help="Restore the tags a run overwrote.")
    undo.add_argument("run_id")
    undo.add_argument("--dry", action="store_true")

    runs = sub.add_parser("runs", help="Show recent runs.")
    runs.add_argument("--library")
    runs.add_argument("--limit", type=int, default=20)

    failures = sub.add_parser("failures", help="Show items that could not be resolved.")
    failures.add_argument("--library", required=True)
    failures.add_argument("--limit", type=int, default=50)
    failures.add_argument("--retry", action="store_true",
                          help="Clear their backoff so the next run retries them.")

    doctor = sub.add_parser("doctor", help="Validate the config and flag stale genre names.")
    doctor.add_argument("--offline", action="store_true",
                        help="Skip checks that need the network (the MAL genre list).")
    sub.add_parser("schema", help="Print the config JSON Schema (for tooling and web UIs).")

    migrate = sub.add_parser("migrate-config", help="Rewrite a v1 config file in the v2 format.")
    migrate.add_argument("--out", help="Write here instead of stdout.")

    schedule = sub.add_parser("schedule", help="Run on a cron schedule, in the foreground.")
    schedule.add_argument("--cron", default="0 1 * * *",
                          help="Five-field cron expression; the config's schedule block wins.")
    schedule.add_argument("--now", action="store_true", help="Also run once on start.")
    schedule.add_argument("--posters-dir", default="posters")

    serve = sub.add_parser("serve", help="Start the web UI and API (and optionally the scheduler).")
    serve.add_argument("--host", default="127.0.0.1",
                       help="Bind address. Use 0.0.0.0 inside a container.")
    serve.add_argument("--port", type=int, default=8095)
    serve.add_argument("--cron", help="Also run the scheduler in this process.")
    serve.add_argument("--now", action="store_true", help="With --cron: run once on start.")
    serve.add_argument("--posters-dir", default="posters")
    serve.add_argument("--static-dir", help="Built UI directory (defaults to the package's).")

    return parser


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _setup_logging(verbosity: int, *, daemon: bool = False) -> None:
    """WARNING for one-shot commands, INFO for the long-running ones (their
    console *is* the log), DEBUG with -vv. Migration steps always show."""
    level = logging.INFO if daemon else logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # What an upgrade did to the files must be visible whatever the verbosity.
    logging.getLogger("plex_auto_genres.migration").setLevel(min(level, logging.INFO))
    # plexapi is extremely chatty at DEBUG.
    logging.getLogger("plexapi").setLevel(max(level, logging.INFO))
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _confirm(prompt: str) -> bool:
    try:
        while True:
            answer = input(f"{prompt} [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("", "n", "no"):
                return False
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _selected_runs(config: AppConfig, names: list[str] | None, type_override: str | None):
    if not names:
        return [r for r in config.libraries if r.enabled]
    if type_override and len(names) != 1:
        raise ConfigError("--type can only be used with exactly one --library.")

    selected = []
    for name in names:
        found = config.find(name)
        if found is None and type_override:
            # v1 ran any Plex library from the command line alone; keep that.
            found = LibraryRun(library=name, type=MediaType(type_override))
        if found is None:
            raise ConfigError(
                f"Library {name!r} is not in the config. Known: "
                f"{', '.join(r.library for r in config.libraries) or '(none)'}. "
                "Add --type to run it with the defaults for that type."
            )
        selected.append(found)

    if type_override:
        selected = [selected[0].model_copy(update={"type": MediaType(type_override)})]
    return selected


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


class _CliObserver:
    """Progress bars and per-action summaries for a terminal."""

    def __init__(self, style: Style, *, no_progress: bool, as_json: bool) -> None:
        self._style = style
        self._no_progress = no_progress
        self._as_json = as_json
        self._bar: ProgressBar | None = None

    def begin(self, _run, _action: str, _run_id: str, _total: int, pending: int) -> None:
        """Open a fresh bar sized to the items this action will process."""
        self._close()
        if pending:
            # The bar counts the items actually being processed, so it fills
            # to 100% even when most of the library is already cached.
            self._bar = ProgressBar(pending, enabled=not self._no_progress)

    def item(self, _run, outcome) -> None:
        if self._bar is not None:
            self._bar.advance(suffix=outcome.item.title)

    def report(self, report: RunReport) -> None:
        self._close()
        if not self._as_json:
            print_report(report, self._style)

    def _close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


async def cmd_run(args, config: AppConfig, store: Store, style: Style) -> int:
    """Process the selected libraries and their post-processing actions."""
    runs = _selected_runs(config, args.library, args.type)
    if not runs:
        print(style.yellow("No enabled libraries in the config; nothing to do."))
        return 0

    if not args.yes and not args.dry and sys.stdin.isatty():
        names = ", ".join(style.cyan(r.library) for r in runs)
        target = "genre tags" if any(r.use_genres for r in runs) else "collections"
        print(f"About to update {target} for: {names}")
        if not _confirm("Continue?"):
            return 130

    server = await asyncio.to_thread(plex_client.connect, config.plex)
    reports = await run_libraries(
        config, store, server, runs,
        dry_run=args.dry, force=args.force, only=set(args.only or ()),
        posters_dir=args.posters_dir,
        observer=_CliObserver(style, no_progress=args.no_progress, as_json=args.json),
    )

    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2, ensure_ascii=False))
    unfinished = [r for r in reports if r.action in ("genres", "collections")]
    return 1 if any(r.failed or r.deferred for r in unfinished) else 0


async def cmd_query(args, config: AppConfig, style: Style) -> int:
    """Show what the providers return for a title, without writing anything."""
    media_type = MediaType(args.type)
    providers = tuple(args.provider) if args.provider else None
    from .config import DEFAULT_PROVIDERS

    names = providers or DEFAULT_PROVIDERS[media_type]
    title = " ".join(args.title)

    pool = build_providers(names, media_type, config.providers)
    request = LookupRequest(
        title=title, year=args.year, media_type=media_type, use_keywords=args.keywords
    )

    async with pool:
        for provider in pool.providers:
            try:
                result = await provider.resolve(request)
            except PagError as exc:
                print(f"{style.dim(provider.name)}: {style.red(str(exc))}")
                continue

            origin = style.dim(f"[{result.provider}:{result.provider_id}]")
            print(f"\n{style.bold(result.title)}  {origin}")
            if result.url:
                print(f"  {style.dim(result.url)}")
            if result.score is not None:
                print(f"  score: {style.cyan(f'{result.score}/10')}")
            print(f"  raw:      {', '.join(result.genres) or '(none)'}")

            run = config.find(args.library) if getattr(args, "library", None) else None
            rules = config.rules_for(run) if run else None
            if rules is None:
                defaults = config.defaults.get(media_type)
                rules = defaults
            if rules is not None:
                filtered = ", ".join(rules.apply(result.genres)) or "(none)"
                print(f"  filtered: {style.green(filtered)}")
            return 0
    return 1


def _library_name(config: AppConfig, name: str) -> str:
    """The name as the config spells it, so the store's rows match the pipeline's."""
    entry = config.find(name)
    return entry.library if entry is not None else name


def cmd_bind(args, config: AppConfig, store: Store, style: Style) -> int:
    """Pin a Plex item to a provider id."""
    library = _library_name(config, args.library)
    entry = config.find(library)
    if entry is not None:
        allowed = bindable_schemes(entry.type, entry.resolved_providers)
        if args.provider not in allowed:
            print(style.red(f"Nothing reading {library} can resolve a {args.provider!r} id."))
            print(style.dim(
                f"  It reads {' -> '.join(entry.resolved_providers)}, "
                f"which take: {', '.join(allowed)}."
            ))
            return 1
    store.set_binding(library, args.title, args.provider, args.provider_id, args.note)
    print(
        f"{style.green('bound')} {style.bold(args.title)} in {library} "
        f"-> {args.provider}://{args.provider_id}"
    )
    pinned = store.get_bindings(library, args.title)
    if len(pinned) > 1:
        print(style.dim(f"  now pinned on {len(pinned)} sources: "
                        f"{', '.join(str(e) for e in pinned)}"))
    print(style.dim("  Its cache entry was cleared; the next run will use this id."))
    return 0


def cmd_unbind(args, config: AppConfig, store: Store, style: Style) -> int:
    """Remove a manual binding."""
    library = _library_name(config, args.library)
    if store.delete_binding(library, args.title, args.provider):
        what = f"{args.provider} id" if args.provider else "bindings"
        print(f"{style.green('removed')} {what} for {args.title} in {library}")
        remaining = store.get_bindings(library, args.title)
        if remaining:
            print(style.dim(f"  still pinned: {', '.join(str(e) for e in remaining)}"))
        return 0
    which = f" for {args.provider}" if args.provider else ""
    print(style.yellow(f"No binding{which} on {args.title!r} in {library!r}."))
    return 1


def cmd_manual(args, config: AppConfig, store: Store, style: Style, as_json: bool) -> int:
    """Decide an item's genres by hand, hand them back, or list the decisions."""
    if args.command == "manuals":
        return _list_manual(store, config, _library_name(config, args.library)
                            if args.library else None, style, as_json)
    library = _library_name(config, args.library)
    entry = config.find(library)
    noun = _noun(config, library)
    if args.command == "unmanual":
        decided = store.manual_for_library(library)
        found = _find_item(store, library, args.title, style, within=set(decided))
        if found is None:
            return 1
        key, title = found
        store.delete_manual(library, key)
        print(f"{style.green('handed back')} {style.bold(title)} in {library}: "
              f"the sources decide its {noun} again from the next run")
        if noun == "collections" or (entry is not None and not entry.clear_genres):
            print(style.dim("  This library merges, so names the decision added stay on the "
                            "item; refuse them with --remove first to take them off."))
        return 0

    try:
        added, removed = clean_names(args.add), clean_names(args.remove)
        check_decision(added, removed, config.plex.collection_prefix)
    except ValueError as exc:
        print(style.red(f"{exc}."))
        return 1
    if not (added or removed or args.lock):
        print(style.yellow("Nothing to decide: give --add, --remove or --lock."))
        return 1
    found = _find_item(store, library, args.title, style)
    if found is None:
        return 1
    key, title = found
    before = store.manual_for_library(library).get(key)
    note = args.note if args.note is not None else (before.note if before else None)
    store.set_manual(library, key, added=added, removed=removed, locked=args.lock,
                     note=note or None, title=title if title != key else None)
    after = ManualTags(tuple(added), tuple(removed), args.lock)
    print(f"{style.green('decided')} {style.bold(title)} in {library}: "
          f"{_describe_manual(after, noun)}")
    if before is not None and (before.added, before.removed, before.locked) != (
        after.added, after.removed, after.locked
    ):
        print(style.dim(f"  replaces: {_describe_manual(before, noun)}"))
    print(style.dim("  The next run applies this."))
    return 0


def _find_item(
    store: Store, library: str, text: str, style: Style, *, within: set[str] | None = None
) -> tuple[str, str] | None:
    """The ``(media_key, title)`` a typed title or key names, or None once said why.

    Most items are keyed by a GUID, so a bare title stored as the key would
    match nothing and be ignored without a word; it is looked up instead.
    """
    if not text.strip():
        print(style.yellow("Give the item's title, or its key as the web UI shows it."))
        return None
    found = store.find_keys(library, text)
    if within is not None:
        found = [(key, title) for key, title in found if key in within]
    if len(found) == 1:
        return found[0]
    if found:
        print(style.yellow(f"{text!r} could be {len(found)} items in {library}; "
                           "give the key of the one you mean:"))
        for key, title in found:
            print(f"  {key:30} {title}")
        return None
    guid = ExternalId.parse(text.strip())
    if within is None and guid is not None and guid.scheme in KNOWN_GUID_SCHEMES:
        # A key the web UI shows, for an item no run has reached yet. Spelt the
        # way the pipeline spells keys, or it would never be found.
        print(style.dim(f"  {guid} has not been seen in a run yet; this applies once an "
                        "item keyed that way turns up."))
        return str(guid), str(guid)
    if within is not None:
        print(style.yellow(f"No genres decided by hand on {text!r} in {library!r}."))
    else:
        print(style.yellow(
            f"No item called {text!r} has been seen in {library!r} yet. Run the library "
            "once, or give the item's key as the web UI shows it (e.g. tmdb://1234)."
        ))
    return None


def _noun(config: AppConfig, library: str) -> str:
    """What a library's decisions are about: its genres, or its collections."""
    entry = config.find(library)
    return "collections" if entry is not None and not entry.use_genres else "genres"


def _describe_manual(tags: ManualTags, noun: str = "genres") -> str:
    """One line for a decision, in the terms the console uses."""
    if tags.locked and noun == "genres":
        return f"exactly {', '.join(tags.added)}" if tags.added else "no genres at all"
    parts = [f"+{a}" for a in tags.added] + [f"-{r}" for r in tags.removed]
    shown = ", ".join(parts) or "nothing added"
    return f"sources not asked; {shown}" if tags.locked else shown


def _list_manual(
    store: Store, config: AppConfig, library: str | None, style: Style, as_json: bool
) -> int:
    entries = store.list_manual(library)
    if as_json:
        print(json.dumps([
            {"library": lib, "media_key": key, **tags.as_dict()} for lib, key, tags in entries
        ], indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print(style.dim("No genres decided by hand."))
        return 0
    for lib, key, tags in entries:
        note = f"  {style.dim(tags.note)}" if tags.note else ""
        name = f"{tags.title} {style.dim(key)}" if tags.title else key
        print(f"{lib:20} {name}  {_describe_manual(tags, _noun(config, lib))}{note}")
    return 0


def cmd_bindings(args, config: AppConfig, store: Store, style: Style, as_json: bool) -> int:
    """List manual bindings."""
    rows = store.list_bindings(_library_name(config, args.library) if args.library else None)
    if as_json:
        print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(style.dim("No manual bindings."))
        return 0
    for row in rows:
        note = f"  {style.dim(row['note'])}" if row["note"] else ""
        print(f"{row['library']:20} {row['media_key']:40} -> "
              f"{row['provider']}://{row['provider_id']}{note}")
    return 0


async def cmd_undo(args, config: AppConfig, store: Store, style: Style) -> int:
    """Restore the tag values a previous run overwrote."""
    row = store.get_run(args.run_id)
    if row is None:
        print(style.red(f"No run with id {args.run_id!r}. See 'plex-auto-genres runs'."))
        return 1
    if row["undone_at"]:
        print(style.yellow("That run was already undone."))
        return 1

    snapshots = store.snapshots_for(args.run_id)
    if not snapshots:
        print(style.yellow("That run recorded no changes, so there is nothing to undo."))
        return 0

    print(f"About to restore {style.bold(str(len(snapshots)))} item(s) "
          f"in {style.cyan(row['library'])} to their pre-run tags.")
    if not args.dry and sys.stdin.isatty() and not _confirm("Continue?"):
        return 130

    server = await asyncio.to_thread(plex_client.connect, config.plex)
    restored, skipped = await asyncio.to_thread(
        undo_run, server, store, args.run_id, dry_run=args.dry
    )
    print(f"{style.green(str(restored))} restored, {skipped} skipped.")
    return 0


def cmd_runs(args, store: Store, style: Style, as_json: bool) -> int:
    """Print the run history."""
    rows = store.recent_runs(args.limit, args.library)
    if as_json:
        print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(style.dim("No runs recorded yet."))
        return 0
    print(f"{'RUN ID':14} {'WHEN':17} {'LIBRARY':20} {'ACTION':18} RESULT")
    for row in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"]))
        report = json.loads(row["report"]) if row["report"] else {}
        # The same ladder the API uses; the CLI cannot know whether an open
        # row belongs to a live process, so it never says "running".
        status = run_status(row, live=False)
        if status in ("undone", "cancelled"):
            result = style.yellow(status)
        elif status == "interrupted":
            result = style.red(status)
        else:
            result = (f"{report.get('written', 0)} written, "
                      f"{report.get('failed', 0)} failed")
            if report.get("deferred", 0):
                result += f", {report['deferred']} deferred"
            if report.get("error"):
                result += style.red(f"  {report['error']}")
        flag = style.dim(" (dry)") if row["dry_run"] else ""
        print(f"{row['run_id']:14} {when:17} {row['library']:20} "
              f"{row['action'] + flag:18} {result}")
    return 0


def cmd_failures(args, store: Store, style: Style, as_json: bool) -> int:
    """Print the items a library could not resolve."""
    rows = store.failures(args.library, args.limit)
    if as_json:
        print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(style.green(f"No failures recorded for {args.library}."))
        return 0
    for row in rows:
        title = f"{row['title']} ({row['year']})" if row["year"] else row["title"]
        attempts = style.dim(f"attempt {row['attempts']}")
        print(f"{style.red('x')} {style.bold(title)}  {attempts}")
        print(f"    {row['last_error']}")
    print(style.dim(
        f"\n{len(rows)} shown. Pin a correct id with: "
        f"plex-auto-genres bind '{args.library}' '<title>' tmdb <id>"
    ))
    if args.retry:
        cleared = store.clear_failures(args.library)
        print(style.green(
            f"Cleared {cleared} failed entries; the next run retries them. "
            "Everything that already succeeded is untouched."
        ))
    return 0


def cmd_doctor(
    config_path: str, store: Store, style: Style, as_json: bool = False, *, offline: bool = False
) -> int:
    """Check the config, the credentials and the anime genre names."""
    report = run_doctor(config_path, store, check_taxonomy=not offline)
    if as_json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        return 0 if report.ok else 1
    _print_doctor(report, style)
    return 0 if report.ok else 1


def _print_doctor(report: DoctorReport, style: Style) -> None:
    badge = {"ok": style.green("OK"), "warn": style.yellow("!!"), "error": style.red("!!")}
    for check in report.checks:
        line = f"{badge[check.level]} {check.title}"
        if check.detail and check.level == "ok":
            line += style.dim(f" ({check.detail})")
        print(line)
        if check.detail and check.level != "ok":
            print(f"   {check.detail}")
        for item in check.items:
            print(f"     {item}")
    print()
    if report.errors:
        print(style.red(f"{report.errors} error(s), {report.warnings} warning(s)."))
    elif report.warnings:
        print(style.yellow(f"{report.warnings} thing(s) to look at."))
    else:
        print(style.green("Everything looks fine."))


def cmd_migrate_config(config_path: str, out: str | None, style: Style) -> int:
    """Rewrite a v1 config file in the v2 format."""
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if int(raw.get("version", 1)) >= CONFIG_VERSION:
        print(style.yellow(f"{config_path} is already version {raw['version']}."))
        return 0
    migrated = migrate_v1(raw)
    AppConfig.model_validate({**migrated, "plex": {}, "providers": {}})  # validate before writing
    text = json.dumps(migrated, indent=4, ensure_ascii=False)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(style.green(f"Wrote {out}."))
    else:
        print(text)
    return 0


async def cmd_schedule(args, config_path: str, db_path: str, style: Style) -> int:
    """Run on a schedule in the foreground, replacing the container's crond.

    The config's ``schedule`` block (editable from the UI) overrides ``--cron``
    and can pause the pass; both are re-read while running.
    """
    try:
        validate_cron(args.cron)
    except ValueError as exc:
        print(style.red(str(exc)))
        return 2

    print(f"Scheduler started. Fallback cron: {style.cyan(args.cron)}")
    # The loop re-plans every minute; say each state once.
    announced: SchedulePlan | None = None

    def announce(plan: SchedulePlan) -> None:
        nonlocal announced
        if announced is not None and (plan.cron, plan.enabled, plan.next_fire_at) == (
            announced.cron, announced.enabled, announced.next_fire_at
        ):
            return
        announced = plan
        if plan.next_fire_at is not None:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(plan.next_fire_at))
            print(style.dim(f"Next run at {when} (in {int(plan.next_fire_at - time.time())}s)"))
        elif plan.cron and not plan.enabled:
            print(style.yellow(f"Schedule paused ({plan.cron}); nothing runs until it is resumed."))
        elif plan.cron:
            print(style.red(f"Cron {plan.cron!r} never fires; nothing will run."))
        else:
            print(style.yellow("No schedule configured; nothing will run automatically."))

    cache = CachedConfig(config_path)

    def settings() -> tuple[str | None, bool] | None:
        """The config's schedule block. Raising means "cannot read it right now"."""
        schedule = cache.load().schedule
        return schedule.cron, schedule.enabled

    async def one_pass() -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{style.bold(f'--- scheduled run {stamp} ---')}")
        try:
            config = load_config(config_path)
            with Store(db_path) as store:
                run_args = argparse.Namespace(
                    library=None, type=None, dry=False, force=False, yes=True,
                    no_progress=True, only=None, posters_dir=args.posters_dir, json=False,
                )
                await cmd_run(run_args, config, store, style)
        except PagError as exc:
            print(style.red(f"Run failed: {exc}"))

    scheduler = Scheduler(one_pass, settings=settings, fallback=args.cron, on_plan=announce)
    try:
        await scheduler.run_forever(run_now=args.now)
    except asyncio.CancelledError:
        print("\nScheduler stopped.")
    return 0


def cmd_serve(args, style: Style) -> int:
    """Start uvicorn with the app; the scheduler rides along when --cron is set."""
    import uvicorn

    from .server import create_app
    from .server.auth import AuthSettings, check_bind

    if args.cron:
        try:
            validate_cron(args.cron)
        except ValueError as exc:
            print(style.red(str(exc)))
            return 2

    auth = AuthSettings.from_env()
    check_bind(auth, args.host)  # raises ConfigError; main() prints it
    if not auth.enabled:
        print(style.yellow(
            "No PAG_WEB_PASSWORD set: the console is open to anyone who can reach it."
        ))

    app = create_app(
        args.config, args.db,
        cron=args.cron, run_on_start=args.now,
        posters_dir=args.posters_dir, static_dir=args.static_dir,
        auth=auth,
    )
    print(f"plex-auto-genres UI on {style.cyan(f'http://{args.host}:{args.port}')}"
          f"  (API docs: /api/docs, login: {'required' if auth.enabled else 'off'})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _dispatch(args, store: Store, style: Style) -> int:
    """Run the command that needs the state database open."""
    if args.command in ("run", "serve", "schedule"):
        # A v1 install is brought up to date here, once, and says so. Never
        # fatal: the config is still migrated in memory when it is loaded.
        try:
            migrate_install(args.config, legacy_logs_dir(args.db), store)
        except OSError as exc:
            log.error("Could not complete the v1 upgrade (%s); continuing without it", exc)
    if args.command == "doctor":
        return cmd_doctor(args.config, store, style, args.json, offline=args.offline)
    if args.command in ("bind", "unbind", "bindings"):
        # Bindings are keyed by the config's spelling of the library;
        # the config itself is optional for them, as in v1.
        config = load_config(args.config, missing_ok=True)
        if args.command == "bind":
            return cmd_bind(args, config, store, style)
        if args.command == "unbind":
            return cmd_unbind(args, config, store, style)
        return cmd_bindings(args, config, store, style, args.json)
    if args.command in ("manual", "unmanual", "manuals"):
        return cmd_manual(args, load_config(args.config, missing_ok=True), store, style, args.json)
    if args.command == "runs":
        return cmd_runs(args, store, style, args.json)
    if args.command == "failures":
        return cmd_failures(args, store, style, args.json)
    if args.command == "schedule":
        return asyncio.run(cmd_schedule(args, args.config, args.db, style))
    if args.command == "serve":
        store.close()  # the app's lifespan opens its own connection
        return cmd_serve(args, style)

    # `run --library X --type T` never needed a config file in v1.
    adhoc = args.command == "run" and bool(args.library) and bool(args.type)
    config = load_config(args.config, missing_ok=adhoc or args.command == "query")
    if args.command == "query":
        return asyncio.run(cmd_query(args, config, style))
    if args.command == "undo":
        return asyncio.run(cmd_undo(args, config, store, style))
    return asyncio.run(cmd_run(args, config, store, style))


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and dispatch. Returns the process exit code."""
    load_dotenv()
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    if args.command is None:
        # "run" is the default command; keep the global options typed before it.
        args = parser.parse_args([*raw_argv, "run"])

    _setup_logging(args.verbose, daemon=args.command in ("serve", "schedule"))
    style = Style()

    try:
        if args.command == "schema":
            print(json.dumps(config_json_schema(), indent=2))
            return 0
        if args.command == "migrate-config":
            return cmd_migrate_config(args.config, args.out, style)

        with Store(args.db) as store:
            return _dispatch(args, store, style)

    except KeyboardInterrupt:
        print(style.yellow("\nInterrupted. Progress up to this point has been saved."))
        return 130
    except (ConfigError, PlexConnectionError) as exc:
        print(style.red(str(exc)), file=sys.stderr)
        return 2
    except PagError as exc:
        print(style.red(f"Error: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
