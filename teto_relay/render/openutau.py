"""Stage 5 - headless synthesis through OpenUtau's own engine.

No GUI, no keystroke automation, no second PC. We host OpenUtau.Core in-process
via pythonnet and drive it directly:

    our .ustx (yaml)  ->  UProject built in memory
                      ->  UProject.ValidateFull()      (runs the phonemizer)
                      ->  RenderPhrase.FromPart(...)
                      ->  WorldlineRenderer.Render(...) -> float samples
                      ->  .wav

Two constraints discovered while building this, both worth knowing before
changing anything here:

* `OpenUtau.Core.Format.Ustx.Load` cannot be used. OpenUtau ships a .NET 9
  build of System.IO.Packaging next to a self-contained .NET 8.0.19 runtime;
  loading it throws FileLoadException from inside Ustx.Load. That mismatch is
  latent in the shipped app and only surfaces when hosting Core out-of-process.
  We therefore parse our own YAML - which we wrote - and build UProject through
  the object model instead.
* `RenderEngine` is not a public type, so we call IRenderer directly.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from .. import dotnet
from ..ustx import load_ustx
from ..voicebank import Voicebank
from .base import RenderError

log = logging.getLogger(__name__)

WORLDLINE_SAMPLE_RATE = 44100


def _wait_for_dictionary(phonemizer, timeout: float = 60.0) -> bool:
    """Block until the phonemizer's G2P dictionary has finished loading.

    SetSinger kicks the dictionary load off on a background task and returns
    immediately. Phonemize anything before it lands and every note comes back
    with an empty phoneme string - no error, no warning. On this machine the
    English dictionary takes about 1.5s.
    """
    import clr
    from System.Reflection import BindingFlags

    from .. import dotnet as dotnet_mod

    flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance
    clr_type = clr.GetClrType(type(phonemizer))
    prop = None
    while clr_type is not None and prop is None:
        prop = clr_type.GetProperty("isDictionaryLoading", flags)
        clr_type = clr_type.BaseType
    if prop is None:
        return True  # phonemizers without a dictionary are ready immediately

    began = time.monotonic()
    while time.monotonic() - began < timeout:
        try:
            if not prop.GetValue(phonemizer):
                log.debug("dictionary ready after %.1fs", time.monotonic() - began)
                return True
        except Exception:
            # Throws until the singer is attached; treat that as "still loading".
            pass
        dotnet_mod.drain_ui()
        time.sleep(0.05)

    log.warning("dictionary still loading after %.0fs; phonemes may come back empty", timeout)
    return False


def _int64_field_setter(declaring_type, field_name: str):
    """Compile a typed setter for an Int64 field.

    pythonnet converts every .NET primitive back into a Python int, so there is
    no way to hand `FieldInfo.SetValue` a boxed Int64 - it rejects PyInt, and
    Convert.ToInt64 / Array.GetValue / Convert.ChangeType all round-trip back
    to PyInt. Compiling a lambda whose parameter is typed `long` sidesteps the
    problem: pythonnet marshals Python ints into typed parameters happily.
    """
    import clr
    from System import Action, Int64, Object
    from System.Linq.Expressions import Expression
    from System.Reflection import BindingFlags

    flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance
    field = declaring_type.GetField(field_name, flags)
    if field is None:
        raise RenderError(f"{declaring_type.Name} has no field {field_name!r}")

    target = Expression.Parameter(clr.GetClrType(Object), "target")
    value = Expression.Parameter(clr.GetClrType(Int64), "value")
    body = Expression.Assign(
        Expression.Field(Expression.Convert(target, declaring_type), field), value
    )
    return Expression.Lambda[Action[Object, Int64]](body, target, value).Compile()


def response_type_phonemes(response) -> list:
    """Read the phonemes off an (internal) PhonemizerResponse."""
    from System.Reflection import BindingFlags

    flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance
    field = response.GetType().GetField("phonemes", flags)
    if field is None:
        return []
    groups = field.GetValue(response)
    return [list(g) for g in groups] if groups is not None else []


def _has_response(part) -> str:
    """Whether the runner has delivered a phonemizerResponse (private field)."""
    try:
        import clr
        from System.Reflection import BindingFlags

        from OpenUtau.Core.Ustx import UVoicePart

        flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance
        field = clr.GetClrType(UVoicePart).GetField("phonemizerResponse", flags)
        if field is None:
            return "<no such field>"
        value = field.GetValue(part)
        return "none" if value is None else "present"
    except Exception:
        return "<unreadable>"


def _wait_for_runner() -> None:
    """Block until PhonemizerRunner's queue drains.

    Both the runner property on DocManager and the PhonemizerRunner type itself
    are internal, so this goes through reflection rather than the public API.
    """
    try:
        import clr
        from System.Reflection import BindingFlags

        from OpenUtau.Core import DocManager

        flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance
        prop = clr.GetClrType(DocManager).GetProperty("PhonemizerRunner", flags)
        if prop is None:
            return
        runner = prop.GetValue(DocManager.Inst)
        if runner is None:
            return
        wait = runner.GetType().GetMethod("WaitFinish", flags)
        if wait is not None:
            wait.Invoke(runner, None)
            log.debug("PhonemizerRunner finished")
    except Exception:
        log.debug("could not wait for PhonemizerRunner", exc_info=True)


class OpenUtauRenderer:
    name = "openutau"

    def __init__(self, cfg, bank: Voicebank | None):
        if bank is None:
            raise RenderError("the OpenUtau backend needs a voicebank")
        self.cfg = cfg
        self.bank = bank

        dotnet.start(cfg.openutau_dir)
        self._register_singer_path(bank)
        self.singer = self._load_singer(bank)
        self.renderer = self._make_renderer(cfg.renderer)
        # Built once and reused: constructing it waits on the async dictionary
        # load, which would otherwise cost ~1.3s on every single utterance.
        self._phonemizer = self._make_phonemizer(cfg.phonemizer or bank.phonemizer)
        log.info("OpenUtau backend ready: singer=%s renderer=%s", self.singer.Name, self.renderer)

    # ------------------------------------------------------------ singer
    def _register_singer_path(self, bank: Voicebank) -> None:
        """Point OpenUtau's singer search at the bank's parent directory.

        Done in memory only - we never write OpenUtau's preferences file, so
        this leaves the user's install untouched.
        """
        search_root = str(Path(self.cfg.voicebank_root).resolve())
        try:
            from OpenUtau.Core import Preferences

            Preferences.Load()
            paths = Preferences.Default.SingerSearchPaths
            if search_root not in list(paths):
                paths.Add(search_root)
            log.debug("singer search paths: %s", list(paths))
        except Exception:
            log.debug("could not extend SingerSearchPaths; will load the bank directly", exc_info=True)

    def _load_singer(self, bank: Voicebank):
        """Resolve a USinger for the selected bank.

        Neither of OpenUtau's discovery routes works from a hosted process:

        * `VoicebankLoader(path).SearchAll()` returns zero banks at every depth
          tried (the bank root, its parent, and the search root).
        * `SingerManager.SearchAllSingers()` throws, because it resolves paths
          through the PathManager singleton, which reads preferences that do
          not exist until the OpenUtau GUI has been run once.

        So we build the Voicebank object ourselves and drive the static loader
        directly. That is the same code path SearchAll would have used, minus
        the discovery step we do not need - we already know exactly where the
        bank is.
        """
        from OpenUtau.Classic import ClassicSinger, Voicebank, VoicebankLoader

        character = bank.root / "character.txt"
        if not character.exists():
            raise RenderError(f"{bank.root} has no character.txt")

        voicebank = Voicebank()
        voicebank.BasePath = str(bank.root)
        voicebank.File = str(character)
        voicebank.Id = bank.root.name
        voicebank.Name = bank.name

        try:
            VoicebankLoader.LoadVoicebank(voicebank)
        except Exception as exc:  # noqa: BLE001
            raise RenderError(f"LoadVoicebank failed for {bank.root}: {exc}") from exc

        # LoadVoicebank populates OtoSets from the sub-bank directories. If it
        # came back empty the bank is laid out in a way it did not expect, and
        # rendering would silently produce nothing.
        oto_sets = list(voicebank.OtoSets or [])
        if not oto_sets:
            log.warning("LoadVoicebank found no oto sets; loading them explicitly")
            for sub in bank.subbanks:
                try:
                    VoicebankLoader.LoadOtoSets(voicebank, str(sub.path))
                except Exception:  # noqa: BLE001
                    log.debug("LoadOtoSets failed for %s", sub.path, exc_info=True)
            oto_sets = list(voicebank.OtoSets or [])

        singer = ClassicSinger(voicebank)
        singer.EnsureLoaded()

        otos = list(singer.Otos or [])
        errors = list(singer.Errors or [])
        if errors:
            log.warning("voicebank reported %d error(s): %s", len(errors), [str(e) for e in errors[:3]])
        if not otos:
            raise RenderError(
                f"Voicebank {bank.root} loaded but exposed no oto entries "
                f"({len(oto_sets)} oto set(s) found). Rendering would produce silence."
            )

        log.info(
            "Loaded singer %r: %d oto entries across %d set(s), type=%s",
            singer.Name,
            len(otos),
            len(oto_sets),
            singer.SingerType,
        )
        return singer

    def _make_renderer(self, name: str):
        from OpenUtau.Core.Render import Renderers

        renderer = Renderers.CreateRenderer(name)
        if renderer is None:
            supported = list(Renderers.GetSupportedRenderers(self.singer.SingerType))
            raise RenderError(f"renderer {name!r} unavailable; this singer supports {supported}")
        return renderer

    # ------------------------------------------------------------- project
    def _build_project(self, doc: dict):
        """Rebuild our .ustx document as an in-memory UProject."""
        from OpenUtau.Core.Format import Ustx
        from OpenUtau.Core.Ustx import UProject, URenderSettings, UTrack, UVoicePart

        project = UProject()
        # A bare UProject has an EMPTY expressions dictionary. RenderPhone's
        # constructor reads expressions per phoneme, so without this every
        # phrase dies with a NullReferenceException inside FromPart - long
        # after the phonemes themselves are perfectly healthy. This is the part
        # of the .ustx `expressions:` block we chose not to hand-write.
        Ustx.AddDefaultExpressions(project)
        project.bpm = float(doc["bpm"])
        project.resolution = int(doc["resolution"])
        project.beatPerBar = int(doc.get("beat_per_bar", 4))
        project.beatUnit = int(doc.get("beat_unit", 4))

        track = UTrack(project)
        track.TrackNo = 0
        track.Singer = self.singer
        track.Phonemizer = self._phonemizer

        # Without RendererSettings, RenderPhrase.FromPart has no renderer to
        # attach to the phrase and silently yields an empty list.
        settings = URenderSettings()
        settings.renderer = doc["tracks"][0].get("renderer_settings", {}).get("renderer") or self.cfg.renderer
        track.RendererSettings = settings

        # Each renderer contributes its own expressions on top of the defaults.
        # WORLDLINE needs a "worldline" entry, and ResamplerItem indexes the
        # dictionary directly - a missing key is a KeyNotFoundException, not a
        # graceful fallback.
        try:
            for descriptor in self.renderer.GetSuggestedExpressions(self.singer, settings):
                project.RegisterExpression(descriptor)
        except Exception:
            log.warning("could not register renderer expressions", exc_info=True)

        project.tracks.Add(track)

        part_doc = doc["voice_parts"][0]
        part = UVoicePart()
        part.trackNo = 0
        part.position = int(part_doc["position"])
        part.Duration = int(part_doc["duration"])

        # Phonetic hints travel alongside the notes: UNote has no field for
        # them, so they are indexed positionally and read back in
        # _phonemize_sync, which builds the Phonemizer.Note structs.
        self._hints = []
        for note_doc in part_doc["notes"]:
            note = project.CreateNote(
                int(note_doc["tone"]),
                int(note_doc["position"]),
                int(note_doc["duration"]),
            )
            note.lyric = str(note_doc["lyric"])
            self._apply_pitch(note, note_doc)
            part.notes.Add(note)
            self._hints.append(note_doc.get("phonetic_hint") or None)

        project.parts.Add(part)
        # voiceParts is a cached view that AfterLoad normally builds; it is null
        # on a hand-built project, and only some code paths consult it.
        if project.voiceParts is not None:
            project.voiceParts.Add(part)

        # ValidateFull() walks the project's own part collections and leaves a
        # hand-built part untouched, so we validate the part explicitly. This
        # is the step that turns "hello" into the X-SAMPA phonemes the
        # voicebank actually contains.
        from OpenUtau.Core import ValidateOptions

        options = ValidateOptions()
        options.SkipTiming = False
        options.SkipPhonemizer = False
        options.SkipPhoneme = False
        options.Part = part

        project.Validate(options)
        track.Validate(options, project)
        part.Validate(options, project, track)

        phonemes = self._await_phonemes(part, project, track, options)
        log.debug(
            "phonemized %d note(s) -> %d phoneme(s): %s",
            len(list(part.notes)),
            len(phonemes),
            [str(p.phoneme) for p in phonemes[:12]],
        )
        if not phonemes:
            raise RenderError(
                f"the phonemizer produced no phonemes for {[str(n.lyric) for n in part.notes][:5]}. "
                f"Check that bank {self.bank.key!r} ({self.bank.flavour}) suits these lyrics - "
                "English words need the en-cvvc bank."
            )
        return project, track, part

    def _phonemize_sync(self, project, track, part) -> bool:
        """Phonemize on this thread, bypassing PhonemizerRunner's background loop.

        The runner never delivers a response in a hosted process - its queue
        accepts requests and its notifications arrive (we drained 398 of them),
        but `part.phonemizerResponse` stays null - and `WaitFinish()` deadlocks
        because it waits on callbacks only we can pump.

        PhonemizerRunner exposes a *static* `Phonemize(request)` that does the
        work inline, so we build the request ourselves and hand the answer to
        `UVoicePart.SetPhonemizerResponse`. Everything here is internal API,
        hence the reflection.
        """
        import clr
        from System import Activator, Array, Int32, Int64
        from System.Reflection import Assembly, BindingFlags

        from OpenUtau.Api import Phonemizer

        flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static
        asm = Assembly.LoadFrom(str(Path(self.cfg.openutau_dir) / "OpenUtau.Core.dll"))
        request_type = asm.GetType("OpenUtau.Api.PhonemizerRequest")
        runner_type = asm.GetType("OpenUtau.Api.PhonemizerRunner")
        note_type = clr.GetClrType(Phonemizer).GetNestedType("Note")
        if None in (request_type, runner_type, note_type):
            raise RenderError("could not reach OpenUtau's internal phonemizer API")

        notes = list(part.notes)
        if not notes:
            return False

        # A group is a run of notes with no gap between them - the phonemizer
        # treats each group as one connected utterance.
        groups: list[list] = [[notes[0]]]
        for previous, note in zip(notes, notes[1:]):
            if note.position == previous.position + previous.duration:
                groups[-1].append(note)
            else:
                groups.append([note])

        # Phonemizer.Note is public, so build it through pythonnet directly.
        # Reflection's FieldInfo.SetValue cannot take a Python int - it rejects
        # PyInt with "cannot be converted to type 'System.Int32'", and
        # Convert.ToInt32 just round-trips back into a Python int.
        note_struct = Phonemizer.Note
        attrs_struct = Phonemizer.PhonemeAttributes
        empty_attrs = Array[attrs_struct]([])

        # Index the hints by note object, since grouping reorders them.
        hint_by_note = {}
        for note, hint in zip(notes, getattr(self, "_hints", [])):
            if hint:
                hint_by_note[id(note)] = hint

        def to_struct(note):
            s = note_struct()
            s.lyric = note.lyric
            # A hint makes the phonemizer use these exact sounds rather than
            # its English dictionary - the only way to sing a word the
            # dictionary does not know, or knows but mispronounces.
            s.phoneticHint = hint_by_note.get(id(note))
            s.tone = note.tone
            s.position = note.position
            s.duration = note.duration
            s.phonemeAttributes = empty_attrs
            return s

        grouped = Array.CreateInstance(note_type.MakeArrayType(), len(groups))
        index_list = []
        cursor = 0
        for i, group in enumerate(groups):
            grouped.SetValue(Array[note_struct]([to_struct(n) for n in group]), i)
            index_list.append(cursor)
            cursor += len(group)
        indexes = Array[Int32](index_list)

        timestamp_field = clr.GetClrType(type(part)).GetField("notesTimestamp", flags)
        timestamp = timestamp_field.GetValue(part) if timestamp_field is not None else 0

        request = Activator.CreateInstance(request_type)
        for name, value in (
            ("singer", self.singer),
            ("part", part),
            ("timestamp", timestamp),
            ("noteIndexes", indexes),
            ("notes", grouped),
            ("phonemizer", track.Phonemizer),
            ("timeAxis", project.timeAxis),
        ):
            field = request_type.GetField(name, flags)
            if field is None:
                raise RenderError(f"PhonemizerRequest has no field {name!r}")
            if name == "timestamp":
                # Must match the part's notesTimestamp or SetPhonemizerResponse
                # discards the result. Needs the compiled typed setter.
                _int64_field_setter(request_type, "timestamp")(request, value)
                continue
            field.SetValue(request, value)

        if self.cfg.explicit_phonemizer_setup:
            try:
                track.Phonemizer.SetUp(grouped, project, track)
                log.debug("phonemizer SetUp complete")
            except Exception:
                log.warning("phonemizer SetUp failed", exc_info=True)

        phonemize = runner_type.GetMethod("Phonemize", flags)
        if phonemize is None:
            raise RenderError("PhonemizerRunner.Phonemize not found")
        response = phonemize.Invoke(None, [request])
        if response is None:
            return False

        # One unknown word poisons every group after it in the same request:
        # "teto" phonemizes fine alone but comes back as "error" when it
        # follows "kasane". Re-run the affected groups on their own so a word
        # the dictionary does not know costs only itself.
        response = self._retry_failed_groups(
            response, request, request_type, runner_type, phonemize, grouped, indexes, flags
        )

        # What did the phonemizer actually return?
        try:
            groups_out = response_type_phonemes(response)
            log.debug(
                "response carries %d group(s): %s",
                len(groups_out),
                [[str(p.phoneme) for p in g] for g in groups_out[:3]],
            )
        except Exception:
            log.debug("could not inspect the response", exc_info=True)

        # SetPhonemizerResponse is internal, so it too goes through reflection.
        setter = clr.GetClrType(type(part)).GetMethod("SetPhonemizerResponse", flags)
        if setter is None:
            raise RenderError("UVoicePart.SetPhonemizerResponse not found")
        setter.Invoke(part, [response])

        log.debug("phonemized %d note(s) in %d group(s) synchronously", len(notes), len(groups))
        return True

    def _retry_failed_groups(
        self, response, request, request_type, runner_type, phonemize, grouped, indexes, flags
    ):
        """Re-phonemize any group that came back as "error", on its own.

        Phonemize processes every group in one request, and a word it cannot
        look up takes the following groups down with it. Retrying the failures
        individually recovers the words that were only collateral damage.
        """
        from System import Activator, Array

        response_type = response.GetType()
        phonemes_field = response_type.GetField("phonemes", flags)
        if phonemes_field is None:
            return response

        groups = list(phonemes_field.GetValue(response))
        failed = [
            i
            for i, g in enumerate(groups)
            if any(str(p.phoneme) == "error" for p in g)
        ]
        # Single-group requests are retried too: the phonemizer is
        # intermittently flaky when driven this way, and a word that failed
        # once usually succeeds on a fresh request. Without this, a transient
        # failure silently drops the word.
        if not failed:
            return response

        recovered = 0
        for i in failed:
            try:
                single = Activator.CreateInstance(request_type)
                for name in ("singer", "part", "phonemizer", "timeAxis"):
                    field = request_type.GetField(name, flags)
                    field.SetValue(single, request_type.GetField(name, flags).GetValue(request))
                _int64_field_setter(request_type, "timestamp")(
                    single, request_type.GetField("timestamp", flags).GetValue(request)
                )
                note_type = grouped.GetValue(i).GetType().GetElementType()
                one = Array.CreateInstance(note_type.MakeArrayType(), 1)
                one.SetValue(grouped.GetValue(i), 0)
                request_type.GetField("notes", flags).SetValue(single, one)
                request_type.GetField("noteIndexes", flags).SetValue(
                    single, Array[type(indexes.GetValue(0))]([indexes.GetValue(i)])
                )

                retry = phonemize.Invoke(None, [single])
                if retry is None:
                    continue
                retry_groups = list(phonemes_field.GetValue(retry))
                if retry_groups and not any(str(p.phoneme) == "error" for p in retry_groups[0]):
                    groups[i] = retry_groups[0]
                    recovered += 1
            except Exception:
                log.debug("retry failed for group %d", i, exc_info=True)

        if recovered:
            log.debug("recovered %d/%d group(s) by retrying individually", recovered, len(failed))
            element = groups[0].GetType() if groups else None
            if element is not None:
                merged = Array.CreateInstance(element, len(groups))
                for i, g in enumerate(groups):
                    merged.SetValue(g, i)
                phonemes_field.SetValue(response, merged)
        return response

    def _map_otos(self, part, project, track) -> list:
        """Resolve each phoneme to a voicebank sample.

        Validate only maps the leading phoneme of the part; the rest come back
        with a null `oto`, and RenderPhone dereferences it unconditionally, so
        FromPart dies with a NullReferenceException. UPhoneme exposes the
        per-phoneme validation steps publicly, so we drive them ourselves.
        """
        import clr
        from System.Reflection import BindingFlags

        from OpenUtau.Core.Ustx import UPhoneme

        # These are internal, so pythonnet does not surface them as attributes.
        flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance
        phoneme_type = clr.GetClrType(UPhoneme)
        validate_oto = phoneme_type.GetMethod("ValidateOto", flags)
        validate_duration = phoneme_type.GetMethod("ValidateDuration", flags)
        validate_overlap = phoneme_type.GetMethod("ValidateOverlap", flags)
        validate_envelope = phoneme_type.GetMethod("ValidateEnvelope", flags)

        phonemes = list(part.phonemes or [])
        unmapped = 0
        for phoneme in phonemes:
            note = phoneme.Parent
            if note is None:
                continue
            for method, args in (
                (validate_oto, [track, note]),
                (validate_duration, [project, part]),
                (validate_overlap, [project, track, part, note]),
                (validate_envelope, [project, track, note]),
            ):
                if method is None:
                    continue
                try:
                    method.Invoke(phoneme, args)
                except Exception:
                    log.debug(
                        "%s failed for phoneme %r", method.Name, str(phoneme.phoneme), exc_info=True
                    )
            if phoneme.oto is None:
                unmapped += 1

        if unmapped:
            # "error" is the phonemizer's own sentinel for a word it could not
            # look up - a dictionary gap, not a voicebank gap. The two have
            # completely different fixes, so report them separately.
            unknown_words, missing_samples = [], []
            for p in phonemes:
                if p.oto is not None:
                    continue
                word = str(p.Parent.lyric) if p.Parent is not None else "?"
                if str(p.phoneme) == "error":
                    unknown_words.append(word)
                else:
                    missing_samples.append(f"{str(p.phoneme)!r} in {word!r}")

            if unknown_words:
                log.warning(
                    "Not in the English dictionary, so sung as silence: %s",
                    ", ".join(sorted(set(unknown_words))),
                )
            if missing_samples:
                log.warning(
                    "%d phoneme(s) have no sample in this voicebank: %s",
                    len(missing_samples),
                    "; ".join(missing_samples),
                )
        else:
            log.debug("mapped all %d phoneme(s) to voicebank samples", len(phonemes))
        return phonemes

    def _await_phonemes(self, part, project, track, options, timeout: float = 20.0) -> list:
        """Drive the asynchronous phonemizer to completion.

        `Validate` only *queues* work onto PhonemizerRunner's background loop
        and returns. The runner then stores its answer in the part's
        `phonemizerResponse`, which a *later* Validate applies to `phonemes`.
        So the sequence is: validate, wait for the runner, validate again.

        We drain the UI callback queue around each step because that is the
        channel OpenUtau uses to hand results back out of the runner.
        """
        try:
            self._phonemize_sync(project, track, part)
        except Exception:
            log.warning("synchronous phonemization failed", exc_info=True)

        log.debug(
            "straight after SetPhonemizerResponse: phonemes=%d response=%s",
            len(list(part.phonemes or [])),
            _has_response(part),
        )

        dotnet.drain_ui()
        try:
            # SkipPhonemizer matters: a plain Validate re-runs the phonemizer,
            # which bumps the part's timestamp and discards the response we
            # just handed it. We only want the response applied.
            from OpenUtau.Core import ValidateOptions

            apply_only = ValidateOptions()
            apply_only.SkipTiming = False
            apply_only.SkipPhonemizer = True
            apply_only.SkipPhoneme = False
            apply_only.Part = part
            # The first pass applies the response and maps the leading phoneme
            # of each note; the oto lookup for the remainder settles on a
            # second pass, and RenderPhone dereferences oto unconditionally.
            for _ in range(3):
                part.Validate(apply_only, project, track)
                built = list(part.phonemes or [])
                if built and all(getattr(p, "oto", None) is not None for p in built):
                    break
        except Exception:
            log.debug("post-phonemize validate failed", exc_info=True)

        built = self._map_otos(part, project, track)

        phonemes = list(part.phonemes or [])
        if not phonemes:
            log.warning("no phonemes after synchronous phonemization (response=%s)", _has_response(part))
        return phonemes

    def _make_phonemizer(self, qualified_name: str):
        """Instantiate a phonemizer by its fully-qualified .NET type name.

        PhonemizerFactory.Get takes a Type, not a string, so the simplest route
        is pythonnet's own import machinery - the namespace maps straight onto
        a Python module.
        """
        phonemizer = None
        module_name, _, class_name = qualified_name.rpartition(".")
        if module_name:
            try:
                module = __import__(module_name, fromlist=[class_name])
                phonemizer = getattr(module, class_name)()
            except Exception:
                log.debug("could not import %s directly", qualified_name, exc_info=True)

        if phonemizer is None:
            from System import Activator, Type

            for assembly in ("OpenUtau.Plugin.Builtin", "OpenUtau.Core"):
                clr_type = Type.GetType(f"{qualified_name}, {assembly}")
                if clr_type is not None:
                    phonemizer = Activator.CreateInstance(clr_type)
                    break
        if phonemizer is None:
            raise RenderError(f"phonemizer {qualified_name!r} not found")

        # Bind the singer here, on our own thread, so the dictionary load and
        # its DocManager notifications happen where pythonnet can marshal them.
        # Left to OpenUtau, SetSinger runs inside PhonemizerRunner's background
        # loop, where converting the notification delegate throws
        # "Failed to create Python type for System.Action" and kills the loop -
        # which shows up as zero phonemes rather than an error.
        try:
            phonemizer.SetSinger(self.singer)
            log.debug("phonemizer %s bound to singer on the calling thread", class_name)
        except Exception:
            log.warning("phonemizer SetSinger failed", exc_info=True)

        _wait_for_dictionary(phonemizer)
        return phonemizer

    def _apply_pitch(self, note, note_doc: dict) -> None:
        """Copy our detected contour onto the note's pitch envelope."""
        points = note_doc.get("pitch", {}).get("data") or []
        if len(points) < 2:
            return
        try:
            from OpenUtau.Core.Ustx import PitchPoint, PitchPointShape

            note.pitch.data.Clear()
            for p in points:
                note.pitch.data.Add(PitchPoint(float(p["x"]), float(p["y"]), PitchPointShape.io))
            note.pitch.snapFirst = True
        except Exception:
            log.debug("could not apply the pitch contour; leaving the default", exc_info=True)

    # -------------------------------------------------------------- render
    def render(self, ustx_path: Path, out_wav: Path) -> Path:
        from System.Threading import CancellationTokenSource

        doc = load_ustx(ustx_path)
        project, track, part = self._build_project(doc)

        from OpenUtau.Core.Render import RenderPhrase

        phrases = list(RenderPhrase.FromPart(project, track, part))
        if not phrases:
            raise RenderError(f"{ustx_path.name} produced no render phrases (phonemizer returned nothing)")

        cancellation = CancellationTokenSource()
        segments: list[tuple[float, np.ndarray]] = []
        for phrase in phrases:
            task = self.renderer.Render(phrase, self._progress(), 0, cancellation, False)
            result = task.Result  # blocking; we are already on a worker thread
            samples = np.fromiter(result.samples, dtype=np.float32) if result.samples is not None else np.empty(0)
            if samples.size:
                segments.append((float(result.positionMs) - float(result.leadingMs), samples))

        if not segments:
            raise RenderError(f"{ustx_path.name} rendered no audio")

        mixed = self._mix(segments)
        out_wav = Path(out_wav)
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_wav, mixed, WORLDLINE_SAMPLE_RATE)
        log.info("Rendered %s (%.2fs, %d phrase(s))", out_wav.name, len(mixed) / WORLDLINE_SAMPLE_RATE, len(phrases))
        return out_wav

    def _progress(self):
        from OpenUtau.Core.Render import Progress

        return Progress(0)

    def _mix(self, segments: list[tuple[float, np.ndarray]]) -> np.ndarray:
        """Lay each phrase at its own offset and sum."""
        total = max(
            int(offset_ms / 1000.0 * WORLDLINE_SAMPLE_RATE) + len(samples) for offset_ms, samples in segments
        )
        buffer = np.zeros(max(total, 1), dtype=np.float64)
        for offset_ms, samples in segments:
            start = max(0, int(offset_ms / 1000.0 * WORLDLINE_SAMPLE_RATE))
            end = start + len(samples)
            if end > len(buffer):
                buffer = np.pad(buffer, (0, end - len(buffer)))
            buffer[start:end] += samples
        peak = float(np.max(np.abs(buffer)))
        if peak > 1.0:
            buffer /= peak
        return buffer.astype(np.float32)

    def close(self) -> None:
        pass
