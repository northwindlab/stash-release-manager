"""Milestone 3 (2026-09-19): sidecar-owned indexer search + release
selection, replacing Whisparr's own automatic search+grab.

Calls Whisparr's own `GET /release?movieId=X` ("Interactive Search") to
reach its 3 configured indexers -- confirmed live this needs no
indexer/Prowlarr credentials of our own, since a Prowlarr-proxied
candidate's `downloadUrl` already embeds Prowlarr's own auth token (see
whisparr_client.py's get_release()). Selection then replicates the
SHAPE of Whisparr's own `DownloadDecisionComparer` comparer chain
(quality rank -> indexer priority -> age -> size -- confirmed against
its actual source during Milestone 3's planning, see PROGRESS.md),
deliberately simplified in two ways agreed with the user during
planning:

- No custom-format scoring -- this project has no such concept.
- "Largest file wins" as the final tiebreak, instead of Whisparr's own
  runtime x preferred-size target (this project doesn't reliably have
  per-scene runtime data to compute that target against).

Whisparr's own `rejected`/`rejections` on each candidate reflect ITS
currently-assigned quality profile, not this project's own
`quality_profile` -- deliberately ignored here in favor of filtering
against the caller-supplied profile directly.

Cutoff is intentionally NOT used for selection: confirmed against
Whisparr's own `UpgradableSpecification` source that cutoff only
governs whether an *already-downloaded* file needs upgrading, never
which candidate a fresh grab picks (`CompareQuality` just takes the
highest-ranked allowed quality in the profile's own ordering,
regardless of cutoff). A future upgrade-search feature would consult
it; a fresh grab never does.

VR is a special case, added 2026-09-19 after checking live what
Whisparr's own "VR" quality profile actually contains: it's NOT a
resolution class -- confirmed via `GET /qualitydefinition` that
Whisparr's "VR" quality (id 32) has its own `source: "vr"` (distinct
import history
from "web"/"bluray"/"hdtv"/etc.) and is always reported at resolution
1080 regardless of the release's real video resolution. Whisparr's own
VR profile allows ONLY this one quality, nothing else. A resolution-
only allow-list (this project's own `quality_profile`) can't express
that distinction -- every ordinary 1080p WEBDL/Bluray release also
reports resolution 1080, so filtering a VR-flagged scene by
`resolutions` alone would happily grab a normal 2D release. `_select_best()`
therefore takes an explicit `require_vr` flag: when set, it filters by
`quality.source == "vr"` instead of the profile's resolution list
entirely (there's nothing meaningful to rank within VR beyond that --
Whisparr's own real VR profile has exactly one allowed tier).

Title plausibility check, added 2026-09-19 after a real near-miss found
during live pre-grab verification (never actually grabbed, caught by
inspecting raw `/release` output by hand before enabling this for
real): Whisparr's own `/release?movieId=X` search does NOT reliably
return results scoped to the target movie's actual scene -- confirmed
live for this instance's "Academy POV" studio, where two different
movies ("Winner's Reward" and "Poolside Foursome Orgy") returned the
EXACT SAME 53 candidates, none of them actually titled anything like
"Poolside Foursome Orgy". This matches this project's own pre-existing
"Boosting Morale"/"Summer Camp Rules" mismatch incident (see
PROGRESS.md, the reason fix_mismatched_download()/verify_downloaded_file()
exist at all) -- known behavior for this Whisparr/indexer combination,
not a one-off. `_select_best()` therefore also requires each candidate's
own release title to plausibly match the target scene's title
(`_title_plausible()`) BEFORE it's even considered for quality/priority/
age/size ranking -- this runs before StashDB-fingerprint verification
(which only happens after a file is already downloaded and imported,
see verification.py) ever gets a chance to catch a wrong grab, so it's
the only line of defense against grabbing entirely the wrong scene.
"""
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import download_client_repo
import download_history_cleanup
import history
import other_scene_check
import release_repo
import torrent_estimate
from qbittorrent_client import AlreadyAdded, QbittorrentClient, QbittorrentError
from sabnzbd_client import SabnzbdClient

logger = logging.getLogger(__name__)

# Common filler words that appear in scene titles but carry no
# identifying signal -- excluded from the significant-word overlap
# check below so e.g. "The Sexy Freeloader" isn't scored down just for
# containing "the".
_TITLE_STOPWORDS = {
    "a", "an", "the", "and", "or", "in", "on", "of", "to", "is", "at",
    "for", "with", "by", "her", "his", "my", "your", "its", "it", "s",
}


def _significant_words(title):
    normalized = re.sub(r"[^a-z0-9]+", " ", (title or "").lower())
    return [w for w in normalized.split() if len(w) > 2 and w not in _TITLE_STOPWORDS]


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _date_plausible(scene_date, candidate_title):
    """True if `candidate_title` embeds the scene's own release date --
    an independent-of-wording signal added 2026-09-19 after a real
    false rejection: studios like Mr. LuckyPOV name releases with only
    performer name + date (e.g. "MrLuckyPOV.26.06.19.Zoey.Di.Giacomo...
    "), never the scene's own marketing-style StashDB title
    ("Surprise Squirt For Sexy Slut Zoey Di Giacomo"), so
    `_title_plausible()`'s word-overlap check has nothing to match
    against and rejects a genuinely correct candidate. Requires a full
    `YYYY-MM-DD` `scene_date` -- a bare year (seen live for some older
    scenes) has nothing specific enough to check, so this returns False
    and leaves the decision to `_title_plausible()` alone. Checked
    against both the 2-digit and 4-digit year forms since both appear
    in real release names."""
    if not scene_date:
        return False
    m = _ISO_DATE_RE.match(scene_date)
    if not m:
        return False
    year, month, day = m.groups()
    padded_candidate = " " + re.sub(r"[^a-z0-9]+", " ", (candidate_title or "").lower()) + " "
    return any(f" {y} {month} {day} " in padded_candidate for y in (year, year[2:]))

# A release cannot be older than the scene it claims to be (v0.250.7). Some studios call their newest episode "placeholder title" every time:
# the title matches ALL of them, and the release names carry no date -- three-year-old posts by other girls were taken for the new scene. The
# indexer's age (days since the post) is the proof: a post older than the scene's release date (plus some slack for studios whose date is
# weeks off, and for early posts) belongs to another scene.
PREDATE_SLACK_DAYS = 14


def predates_scene(scene_date, release, today=None):
    """True when `release` (indexer age in days) was posted clearly BEFORE the scene's release date. No full date or no age -> False (no proof)."""
    age = release.get("age") if isinstance(release, dict) else None
    m = _ISO_DATE_RE.match(scene_date or "")
    if age is None or not m:
        return False
    try:
        scene_day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        days_since = ((today or datetime.now(timezone.utc)) - scene_day).days
        return float(age) > days_since + PREDATE_SLACK_DAYS
    except (ValueError, TypeError):
        return False


class AliasName(str):
    """A name a credited performer is also known under (the scene's own "as" credit and full-name aliases from StashDB, v0.218.2): any of them in a
    release name counts like the performer's own name (case: credited "as Kitty Wild", the performer is "Xenia Blondi"). `.of` = the performer's own
    name. Kept inside the `performer_names` list so every check that asks "does the release name a credited performer" sees them; the checks that COUNT
    performers or build queries use primary_names()."""
    def __new__(cls, text, of):
        obj = str.__new__(cls, text)
        obj.of = of
        return obj


def primary_names(performer_names):
    return [n for n in performer_names or [] if not isinstance(n, AliasName)]


def alias_variants(performer):
    """The extra names of one StashDB scene performer entry ({"as", "performer": {"name", "aliases"}}): the "as" credit and the aliases that are full
    names (a space, 7+ characters -- a short single word like "Lexi" would match inside "Alexis")."""
    name = (performer.get("performer") or {}).get("name") or ""
    extras = [performer.get("as")] + list((performer.get("performer") or {}).get("aliases") or [])
    out, seen = [], {re.sub(r"[^a-z0-9]+", "", name.lower())}
    for alt in extras:
        alt = (alt or "").strip()
        key = re.sub(r"[^a-z0-9]+", "", alt.lower())
        if " " in alt and len(alt) >= 7 and key and key not in seen:
            seen.add(key)
            out.append(AliasName(alt, name))
    return out


def _performer_plausible(performer_names, candidate_title):
    """True if at least one of Whisparr's own `performerNames` for this
    movie (e.g. `["Zoey DiGiacomo", "Tyler D."]`, read straight off the
    movie object -- no extra API call) appears in `candidate_title`.
    Added 2026-09-19 as a REQUIRED second signal alongside
    `_date_plausible()`, not a standalone one, after that date-only
    check caused a real wrong grab within hours of shipping: Academy
    POV's own Whisparr movie metadata has "Tempting Teenage Duo" and
    "Stress Relief Before Exam" (two different scenes, different
    performers) both dated 2021-03-04, and `/release?movieId=X` for
    "Tempting Teenage Duo" -- the same wrong-candidate-scoping bug the
    whole title-plausibility check exists for (see module docstring) --
    handed back only "Stress Relief Before Exam" releases, which still
    passed a bare date check. StashDB's own fingerprint verification
    caught the resulting mismatch after the fact (see verification.py),
    but the grab and file move should never have happened. Performer
    names are a far stronger identity signal than a shared calendar
    date, and they're exactly what the studios `_date_plausible()`
    targets DO encode in their filenames (name + date, no scene title)
    -- checked as a compacted (no separators at all) substring so
    "DiGiacomo" matches "Di.Giacomo" regardless of how either side
    splits the name. Any one matching performer is enough (a candidate
    naming only one of several credited performers is common)."""
    if not performer_names:
        return False
    candidate_compact = re.sub(r"[^a-z0-9]+", "", (candidate_title or "").lower())
    return any(_performer_name_in(name, candidate_compact) for name in performer_names)


def _performer_name_in(name, candidate_compact):
    compact_name = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return len(compact_name) > 3 and compact_name in candidate_compact


def _all_performers_plausible(performer_names, candidate_title):
    """True only if EVERY one of Whisparr's own `performerNames` for
    this movie appears in `candidate_title` -- a much stronger identity
    signal than `_performer_plausible()`'s any-one-match, used
    exclusively to decide whether an otherwise-rejected candidate is
    worth surfacing for a human "needs review" decision rather than
    silently discarding it (see `_select_review_candidate()`). Added
    2026-09-19 after a real near-miss: a candidate named all three of a
    movie's credited performers exactly, but Whisparr's own stored
    `releaseDate` for the movie was 17 days off from the actual release
    -- too far for `_date_plausible()` to accept automatically, but
    naming all 3 performers by pure chance is implausible enough that
    it's worth asking a human rather than filing it next to a genuine
    non-match. Requires at least one performer name (an empty/unknown
    list can't be "all matched", so this is always False then)."""
    # The performers' own names only (v0.218.2/.3): an alias can be another person's stage name (case: "Layla Belle" with the alias "Layla Quinn" offered
    # an unrelated release for review); aliases only count where the date also fits (_candidate_plausible()).
    primaries = primary_names(performer_names)
    if not primaries:
        return False
    candidate_compact = re.sub(r"[^a-z0-9]+", "", (candidate_title or "").lower())
    return all(_performer_name_in(name, candidate_compact) for name in primaries)


def _names_distinguishing_performer(scene_title, performer_names, candidate_title):
    """True if the release names one of the scene's performers whose name
    doesn't appear (even partly) in the scene title -- identity evidence that beats
    resolution when ranking (2026-09-26: for "Some Series with Host"
    [Host Name, Performer B] the generic "Host.Name.Some.Series.
    ...#.7.Scene.4.1080p" outranked "Some.Series.With.Host.#1_3.Performer B" on resolution alone; "Host" says nothing there, "Performer B" does)."""
    title_words = set(_significant_words(scene_title))
    candidate_compact = re.sub(r"[^a-z0-9]+", "", (candidate_title or "").lower())
    return any(_performer_name_in(name, candidate_compact) for name in performer_names or []
               if not set(_significant_words(name)) & title_words)


def _title_plausible(scene_title, candidate_title, performer_names=None):
    """True if `candidate_title` (a raw release name, e.g.
    "Studio.24.01.02.Performer.Name.Scene.Title.XXX.1080p...") plausibly
    matches `scene_title` (the clean scene title from StashDB) -- a
    majority of the scene title's own significant words must appear as
    substrings in the candidate's normalized title. Deliberately loose
    (substring, not exact word match, and a majority rather than 100%)
    since release names routinely abbreviate/reorder/drop small words
    -- see the module docstring's "Show - Scene 3" vs
    "Show.Episode.3" example, a real accepted match that wouldn't
    survive an exact-match rule. A scene title with no significant
    words at all (rare) is treated as unverifiable and allowed through
    rather than blocking every candidate over nothing to check.

    Words that are part of a credited performer's name don't count
    (2026-09-25, real wrong grabs right after v0.59.0 started querying
    by performer name: "Some Performer - Intro 1" and "Alice in
    Wornderfuck" both "matched" an unrelated 2015 release
    "pml.15.03.25.alice.green..." purely on "alice"/"green" -- the
    performer, not the scene). A performer name in a release says
    nothing about WHICH of their scenes it is; that's the job of
    `_date_plausible()`+`_performer_plausible()`. If only performer
    words were left to check, the title is treated as unverified
    (False), not as unverifiable (True)."""
    words = _significant_words(scene_title)
    if not words:
        return True
    performer_words = set()
    for name in performer_names or []:
        performer_words.update(_significant_words(name))
    words = [w for w in words if w not in performer_words]
    if not words:
        return False
    candidate_normalized = re.sub(r"[^a-z0-9]+", " ", (candidate_title or "").lower())
    matched = sum(1 for w in words if w in candidate_normalized)
    return matched >= (len(words) + 1) // 2


# Release-name tokens that describe the FILE, not the scene -- ignored by
# _foreign_words() (formats, codecs, VR/headset tags, scene-site filler).
_RELEASE_NOISE_WORDS = {
    "xxx", "mp4", "mkv", "avi", "wmv", "web", "webdl", "webrip", "dl", "hevc", "x264", "x265", "h264", "h265",
    "avc", "aac", "hi10p", "10bit", "hdr", "sdr", "uhd", "fhd", "hd", "vr", "vr180", "vr360", "sbs", "3dh", "3dv",
    "180", "190", "200", "360", "180x180", "190x180", "200x180", "360x180", "fisheye", "fisheye190", "mkx200",
    "rf52", "fb360", "oculus", "gearvr", "quest", "psvr", "rift", "vive", "pico", "go", "smartphone", "mobile",
    "original", "remastered", "remaster", "digitally", "hq", "lq", "re", "encode", "reencode", "full", "scene",
    "part", "episode", "ep", "lr", "tb", "mono", "stereo", "the", "and", "with", "his", "her", "for", "you",
    "real", "new", "edition", "pov", "xvid", "divx", "4k", "5k", "6k", "7k", "8k", "sd", "480p", "720p", "1080p", "1440p", "2160p",
}


def _foreign_words(scene_title, performer_names, candidate_title):
    """Words of a release name that neither the scene's title nor its
    performers explain (2026-09-26, found live on an upgrade: the scene
    "New Star Is Cuming" (Lulu Chu, 2022) matched the release
    "VRBANGERS_new_star_is_cuming_hazel_moore_6K..." -- a different
    scene, "New Star Is Cuming: Hazel Moore" (2024) -- because the title
    check only asks whether the scene's words are IN the release name,
    not whether the release name says more). Ignored: the first token
    (the studio/site), the release group after the last '-', numbers,
    dates, resolution/format/headset tags and short words.

    The first token is only skipped as the studio/site when a date follows
    it ("Site.24.01.02.Performer..."), the usual scene naming (2026-09-26,
    real wrong grab: "Performer.A.Some.Series.With.Host.#03" for the Performer B scene -- "Performer" was skipped as the "site", so only "second"
    was left and one foreign word is tolerated)."""
    raw = candidate_title or ""
    if "-" in raw and not re.search(r"\d{4}-\d{2}-\d{2}$", raw):
        raw = raw.rsplit("-", 1)[0] if len(raw.rsplit("-", 1)[1]) <= 12 and " " not in raw.rsplit("-", 1)[1] else raw
    tokens = re.sub(r"[^a-z0-9]+", " ", raw.lower()).split()
    if len(tokens) >= 4 and all(t.isdigit() for t in tokens[1:4]) and len(tokens[1]) in (2, 4):
        tokens = tokens[1:]
    known = set()
    for text in [scene_title or ""] + list(performer_names or []):
        words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
        known.update(words)
        known.add("".join(words))  # "Van Wylde" written as "VanWylde" or vice versa
    # A release that splits a compound the scene writes as one word
    # ("VanWylde" vs "Van.Wylde"): both halves count as known.
    for i in range(len(tokens) - 1):
        if tokens[i] + tokens[i + 1] in known:
            known.update((tokens[i], tokens[i + 1]))
    return [t for t in tokens
            if len(t) > 2 and not t.isdigit() and not re.fullmatch(r"\d+p|\d+k|x\d+|h\d+", t)
            and t not in known and t not in _RELEASE_NOISE_WORDS and t not in _TITLE_STOPWORDS]


_MAX_FOREIGN_WORDS = 1
_RELEASE_DATE_RE = re.compile(r"(?:^|[^0-9])((?:19|20)?\d{2})[ ._-](\d{2})[ ._-](\d{2})(?=[^0-9]|$)")


def _date_contradicts(scene_date, candidate_title):
    """True if the release name carries a date and none of its dates is
    within a day of the scene's own date (2026-09-26: a 2016 release of a
    same-titled scene matched the 2018 one)."""
    m = _ISO_DATE_RE.match(scene_date or "")
    if not m:
        return False
    from datetime import date
    scene_day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    found = []
    for y, mo, d in _RELEASE_DATE_RE.findall(candidate_title or ""):
        year = int(y) + (2000 if len(y) == 2 else 0)
        try:
            found.append(date(year, int(mo), int(d)))
        except ValueError:
            continue
    return bool(found) and all(abs((f - scene_day).days) > 1 for f in found)


def _release_date_delta(scene_date, candidate_title):
    """Days between the scene's date and the closest date the release name carries (None: no usable date in either)."""
    m = _ISO_DATE_RE.match(scene_date or "")
    if not m:
        return None
    from datetime import date
    scene_day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    best = None
    for y, mo, d in _RELEASE_DATE_RE.findall(candidate_title or ""):
        try:
            delta = abs((date(int(y) + (2000 if len(y) == 2 else 0), int(mo), int(d)) - scene_day).days)
        except ValueError:
            continue
        best = delta if best is None else min(best, delta)
    return best


def _studio_variants(studio_name):
    """Compact spellings of the studio ("Some Studio: The Series" -> familystrokesthes... and familystrokes) as release names write them."""
    names = [studio_name or ""]
    base = re.split(r"\s*(?::|\s-\s)\s*", studio_name or "", maxsplit=1)[0]
    if base and base != (studio_name or ""):
        names.append(base)
    return [c for c in (re.sub(r"[^a-z0-9]", "", n.lower()) for n in names) if c]


def _near_date_studio_match(scene_title, scene_date, performer_names, studio_name, candidate_title):
    """A release of the scene's own studio dated ONE day off, whose name holds nothing the scene's title / performers do not explain (v0.218.3,
    case: scene "Layla" 25.09., release "SomeStudio.26.09.26.Layla.XXX.2160p" -- studios date their releases a day apart from the provider). Too
    uncertain for an automatic grab (and a re-encode skips the fingerprint check) -> only ever offered under "Needs review"."""
    delta = _release_date_delta(scene_date, candidate_title)
    if delta is None or delta > 1:
        return False
    m = _RELEASE_DATE_RE.search(candidate_title or "")
    prefix = re.sub(r"[^a-z0-9]", "", (candidate_title or "")[:m.start() + 1].lower()) if m else ""
    variants = _studio_variants(studio_name)
    if not prefix or not any(len(v) >= 5 and (prefix == v or prefix.startswith(v) or v.startswith(prefix)) for v in variants):
        return False
    return not _foreign_words(scene_title, performer_names, candidate_title)


_YEAR_RE = re.compile(r"(?<![0-9])(19[89][0-9]|20[0-3][0-9])(?![0-9])")


def _year_contradicts(scene_title, scene_date, candidate_title):
    """True if the release name carries a year that isn't the scene's
    (2026-09-28, found live: "Some_Performer_-_2021", a 3.9 GB year pack,
    was offered as a possible match for a 2026 Some Performer scene -- it
    names the only performer, so the review rule accepted it). Years that
    are part of the scene title ("Summer 2020") or of a full release date
    handled by _date_contradicts() don't count."""
    m = _ISO_DATE_RE.match(scene_date or "")
    if not m:
        return False
    title_years = set(_YEAR_RE.findall(scene_title or ""))
    stripped = _RELEASE_DATE_RE.sub(" ", candidate_title or "")
    years = [y for y in _YEAR_RE.findall(stripped) if y not in title_years]
    return bool(years) and m.group(1) not in years


_REVIEW_MAX_DAYS_SINGLE_PERFORMER = 45


def _release_date_far_off(scene_date, candidate_title, days):
    """True if the release name carries a (parseable) date and all its
    dates are more than `days` away from the scene's; False if one is
    close; None if there's no parseable date at all (e.g. "06.18.26",
    month first -- not guessed)."""
    m = _ISO_DATE_RE.match(scene_date or "")
    if not m:
        return None
    from datetime import date
    scene_day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    found = []
    for y, mo, d in _RELEASE_DATE_RE.findall(candidate_title or ""):
        year = int(y) + (2000 if len(y) == 2 else 0)
        try:
            found.append(date(year, int(mo), int(d)))
        except ValueError:
            continue
    if not found:
        return None
    return all(abs((f - scene_day).days) > days for f in found)


# Genre words shared by a large part of any performer's releases -- they
# say nothing about WHICH scene (2026-09-28: "Anal Fuck" matched another
# Some Performer scene). Only used for the single-performer review evidence.
_GENRE_WORDS = {
    "anal", "fuck", "fucks", "fucked", "fucking", "sex", "hard", "hardcore", "rough", "pov", "xxx", "teen",
    "teens", "milf", "cum", "cums", "cumming", "creampie", "pussy", "blowjob", "facial", "big", "tits", "ass",
    "girl", "girls", "hot", "sexy", "horny", "slut", "slutty", "babe", "amateur", "homemade", "porn", "video",
}


_PACK_RE = re.compile(r"(?:^|[._\s-])(?:site-?rip|siterip|megapack|mega-pack|pack|collection|compilation|complete)"
                      r"(?:[._\s-]|$)", re.IGNORECASE)


def _single_performer_evidence(scene_title, scene_date, performer_names, candidate_title):
    """Extra evidence a single-performer scene's review candidate needs: a
    release date within 45 days, or at least one significant word of the
    scene title (performer-name words excluded) as whole words -- two of
    them, or the only one of a one-word title."""
    far = _release_date_far_off(scene_date, candidate_title, _REVIEW_MAX_DAYS_SINGLE_PERFORMER)
    if far is not None:
        # A dated release decides by its date alone: within 45 days or not
        # (a 2023 "...Brunette.POV" must not pass on two generic words).
        return not far
    performer_words = set()
    for name in performer_names or []:
        performer_words.update(_significant_words(name))
    words = [w for w in _significant_words(scene_title) if w not in performer_words and w not in _GENRE_WORDS]
    tokens = set(re.sub(r"[^a-z0-9]+", " ", (candidate_title or "").lower()).split())
    # Two shared words (one for a one-word title): a single generic word
    # ("rough") is shared by half the performer's releases.
    return len({w for w in words if w in tokens}) >= min(2, len(set(words)))


def _candidate_plausible(scene_title, scene_date, performer_names, candidate_title):
    """The single combined gate `_select_best()`/`search_and_grab()`
    both use: a candidate is accepted if EITHER its own title plausibly
    matches the scene's StashDB title, OR its filename embeds both the
    scene's release date AND at least one credited performer's name, OR
    (v0.213.5, _date_title_plausible()) the exact date plus two title words --
    date alone is deliberately NOT sufficient (see `_performer_plausible()`'s
    docstring for the real wrong-grab this combination was added to
    prevent, 2026-09-19)."""
    if _year_contradicts(scene_title, scene_date, candidate_title):
        return False
    date_and_performer = (_date_plausible(scene_date, candidate_title)
                          and _performer_plausible(performer_names, candidate_title))
    if date_and_performer:
        return True
    if _date_title_plausible(scene_title, scene_date, performer_names, candidate_title):
        return True
    # Title match only counts when the release name doesn't ALSO name
    # something else (another performer, a different subtitle) -- see
    # _foreign_words(). One unexplained word is tolerated unless the
    # release also carries a different date; a different date ALONE is
    # not enough -- some studios' release dates are weeks or months off
    # from StashDB's (checked 2026-09-26 on two verified AcademyPOV grabs).
    if not _title_plausible(scene_title, candidate_title, performer_names):
        return False
    foreign = len(_foreign_words(scene_title, performer_names, candidate_title))
    if foreign > _MAX_FOREIGN_WORDS:
        return False
    return not (foreign == 1 and _date_contradicts(scene_date, candidate_title))


def _date_title_plausible(scene_title, scene_date, performer_names, candidate_title):
    """Third way in (v0.213.5, tested offline first on 23 stuck scenes / 516 stored candidates: only the compilation below was newly accepted): the release name carries the scene's date AND at least two of the title's significant
    words (the studio name counts as one), and names nothing else that is foreign to the scene. For a studio compilation ("FeetishPOV
    Compilation - Your Tight & Slender Favorites", six performers) the release is only "FeetishPOV.26.08.13.Compilation.XXX...": it names no
    performer (date + performer cannot pass) and carries 2 of the 5 title words (the usual half is 3). Another scene of the same studio and day
    names its own performers / words, which `_foreign_words()` turns down."""
    if not _date_plausible(scene_date, candidate_title):
        return False
    words = _significant_words(scene_title)
    if len(words) < 2:
        return False
    normalized = re.sub(r"[^a-z0-9]+", " ", (candidate_title or "").lower())
    if sum(1 for w in words if w in normalized) < 2:
        return False
    return len(_foreign_words(scene_title, performer_names, candidate_title)) <= _MAX_FOREIGN_WORDS


def _indexer_priorities(whisparr_client):
    """{indexerId: priority}, lower number = preferred -- same
    convention Whisparr's own CompareIndexerPriority sorts on (confirmed
    live 2026-09-19). Read fresh every call, not cached: this project's
    indexer set is tiny (3 today) and rarely changes, not worth a
    cache-invalidation story. Best-effort -- a Whisparr hiccup here just
    means every candidate is treated as equal priority rather than
    failing the whole search."""
    try:
        return {i["id"]: i.get("priority", 25) for i in whisparr_client.get_indexers()}
    except Exception:
        logger.exception("indexer_search: failed to fetch indexer priorities, treating all as equal")
        return {}


_RESOLUTION_RE = re.compile(r"\b(2160|1440|1080|720|576|540|480|360)p\b", re.IGNORECASE)
# Heights below 720 are standard definition: the quality profile's lowest class is 480p (like Whisparr's and the library's naming), so a name
# with 360p / 540p / 576p counts as 480p instead of "no resolution" (which was judged from the size and shown as "~480p (estimated)").
_SD_CLASS_MAX = 576


def name_height(title):
    """The height a release name states ("...540p..." -> 540), None without one."""
    m = _RESOLUTION_RE.search(title or "")
    return int(m.group(1)) if m else None


def resolution_class(height):
    """The quality class of a named height: the height itself from 720 up, 480 for every standard-definition height."""
    return 480 if height is not None and height <= _SD_CLASS_MAX else height
# Standard definition without a "480p" in the name (v0.211.6): an "SD" segment ("Name.SD.XXX.MP4") or the SD variant of a release group
# ("...XXX.MP4-GRPX" next to "...-P0RNL0V3R"). Uppercase only, so words and names cannot trip it. Such a release counts as 480p;
# before, no resolution at all meant "unknown" and let it through to Needs review.
_SD_RE = re.compile(r"(?:^|[._\s-])SD(?:[._\s-]|$)|-[A-Z0-9]{3,}SD$")
# NOT \bVR\b -- confirmed live 2026-09-22 against real VR releases this
# project's own indexers actually carry (e.g.
# "...Oculus.8K_VR"/"SLROriginals...Oculus.4K_VR"): regex \b treats "_"
# as a word character, so there is NO word boundary between "_" and
# "VR" in "8K_VR" -- \bVR\b silently never matches the single most
# common real-world VR release-name suffix on this project's own
# indexers, misclassifying every one of them as non-VR. Real release
# names delimit tag segments with '.'/'_'/space, never mid-word --
# matching VR as a segment bounded by any of those (or start/end of
# string) instead of classic \b is what those titles actually need.
#
# Widened 2026-09-25 (found via the new search log: a real VR release
# "BadoinkVR_..._8K_180_180x180_3dh" was rejected as "not VR"): VR also
# counts as the END of a segment (studio names like "BaDoinkVR"/
# "WankzVR"), with an optional 180/360 suffix ("XXX.VR180.2160p"), and
# '-' is a delimiter too ("VR-wankzvr-..."). Still uppercase-only, so
# plain words can't trip it. Checked against ~1400 real Prowlarr titles
# (VR and non-VR studios): ~400 previously missed VR releases, no
# non-VR studio title flipped.
_VR_RE = re.compile(r"(?:^|[._\s-])[A-Za-z0-9]*VR(?:180|360)?(?:[._\s-]|$)")
# VR projection/lens markers that only VR releases carry, case-
# insensitive (release groups write them either way): "180x180"/
# "190x180"/"360x180", "3dh"/"3dv" (SBS/over-under), "FISHEYE190",
# "MKX200", "RF52", "FB360". Catches lowercase names like
# "wankzvr.rose.carter...LR_FISHEYE190" that _VR_RE can't.
_VR_MARKER_RE = re.compile(
    r"(?:180|190|200|220|360)x180|(?:^|[._\s-])(?:3dh|3dv|fisheye\d*|mkx\d+|rf52|fb360)(?:[._\s-]|$)",
    re.IGNORECASE,
)


# --- Stricter VR candidate rules (2026-09-26, upgrade-search work) ---
# Found live: the old rule accepted any title with a "VR" token, but VR
# STUDIOS carry it in their name ("VRBangers", "BaDoinkVR", "DDF Network
# VR", "VRCosplayX"), so their official 2D versions ("...1080p") passed as
# VR -- 16 of the library's 2D-in-a-VR-scene files are exactly those
# studios. A VR scene's candidate now needs FORMAT evidence, and the user's
# playback limits (Quest 3 / Steam Frame: 6K is the sweet spot, 7K/8K at
# 60 fps stutters; max 15 GB per scene) apply.
_VR_FORMAT_RE = re.compile(
    r"(?:^|[._\s-])(?:LR|TB|SBS|OU|VR180|VR360|180|360|MONO360)(?:[._\s-]|$)", re.IGNORECASE)
_VR_2D_RE = re.compile(r"(?:^|[._\s(\[-])(?:2D|VR2Normal|VR2D|2Normal|Flat)(?:[._\s)\]-]|$)", re.IGNORECASE)
# Headset-targeted encodes ("Oculus", "GearVR", "Oculus.Go", ...) -- only
# ever VR (seen live 2026-09-26 on BaDoinkVR/WankzVR/VRLatina releases).
_VR_DEVICE_RE = re.compile(r"(?:^|[._\s,-])(?:Oculus|GearVR|Quest|PSVR|Rift|Vive|Pico)(?:[._\s,-]|$)", re.IGNORECASE)
# Frame heights 2D never uses (2D: 480/720/1080/1440/2160) -- VR's
# 1920p/2048p/2300p/2900p... are VR evidence on their own.
_FLAT_HEIGHTS = {360, 480, 540, 576, 720, 1080, 1440, 2160}
_VR_HEIGHT_P_RE = re.compile(r"(?:^|[._\s-])(\d{4})p(?:[._\s-]|$)", re.IGNORECASE)
_VR_K_RE = re.compile(r"(?:^|[._\s-])([4-8])K(?:[._\s-]|$)", re.IGNORECASE)
# "nK" in VR naming = total width class -> frame height of the 2:1 frame.
_VR_K_HEIGHT = {4: 1920, 5: 2560, 6: 2880, 7: 3600, 8: 4096}

DEFAULT_VR_RULES = {"max_height": 3456, "max_bytes": 15 * 10**9, "target_bytes": 10 * 10**9,
                    "min_bytes": int(1.5 * 10**9)}


# Size cap for NON-VR releases (2026-09-28, user request after a 147 GB
# "Some_Performer_-_2020" pack came up as a possible match). Checked
# against the library (13,414 scenes): 1080p p99 5.4 GB / max 8.4 GB,
# 2160p p95 8.9 GB / p99 11.4 GB -- so 8 GB in general, 12 GB for 2160p.
DEFAULT_SIZE_LIMITS = {"default": 8 * 10**9, 2160: 12 * 10**9}


def load_size_limits(db):
    """`max_file_gb` / `max_file_gb_2160` from settings (0 = no cap)."""
    from routers.settings import get_setting_value
    def gb(key, default):
        try:
            return int(float(get_setting_value(db, key, "") or default) * 10**9)
        except ValueError:
            return int(default * 10**9)
    return {"default": gb("max_file_gb", 8), 2160: gb("max_file_gb_2160", 12)}


# Torrent choice (v0.231.0): a torrent with fewer seeders than `min_seeders` is a last resort; a lower resolution wins over the best one when the
# best has fewer than `plenty` seeders and the lower one has `factor` times as many.
DEFAULT_TORRENT_CFG = {"min_seeders": 1, "plenty": 10, "factor": 3.0}


def load_torrent_settings(db):
    """`torrent_min_seeders` / `torrent_plenty_seeders` / `torrent_lower_res_factor` from settings."""
    from routers.settings import get_setting_value
    def num(key, default, cast):
        try:
            return cast(float(get_setting_value(db, key, "") or default))
        except ValueError:
            return default
    return {"min_seeders": num("torrent_min_seeders", DEFAULT_TORRENT_CFG["min_seeders"], int),
            "plenty": num("torrent_plenty_seeders", DEFAULT_TORRENT_CFG["plenty"], int),
            "factor": num("torrent_lower_res_factor", DEFAULT_TORRENT_CFG["factor"], float)}


# The length of a scene at its provider (v0.231.0), set by main.py like the parent-studio resolver: fn(provider, scene_id) -> seconds or None.
_duration_resolver = None


def set_duration_resolver(fn):
    global _duration_resolver
    _duration_resolver = fn


def scene_duration(provider, scene_id):
    """Seconds, or None (unknown, no resolver, provider error): the estimates simply stay off then."""
    if _duration_resolver is None:
        return None
    try:
        value = _duration_resolver(provider, scene_id)
        return value if value and value >= torrent_estimate.MIN_DURATION_SEC else None
    except Exception:
        logger.warning("indexer_search: couldn't read the length of %s scene %s", provider, scene_id, exc_info=True)
        return None


def fallback_resolutions(db, scene_date, today=None):
    """Resolutions outside the quality profile that are accepted as a last resort (v0.218.0): once a scene is `fallback_resolution_days` old (0 = never)
    and nothing in the profile turned up -- 2160p (the importer scales it down to 1080p, see transcode.py) and, with `fallback_accept_sd`, SD (480).
    Best first. Empty list: not old enough / switched off."""
    from routers.settings import get_setting_value
    from datetime import date, datetime
    try:
        days = int(float(get_setting_value(db, "fallback_resolution_days", "10") or 0))
    except ValueError:
        days = 0
    if days <= 0 or not scene_date:
        return []
    try:
        released = datetime.strptime(str(scene_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return []
    if ((today or date.today()) - released).days < days:
        return []
    out = [2160]
    if str(get_setting_value(db, "fallback_accept_sd", "true")).lower() == "true":
        out.append(480)
    return out


def too_large(release, is_vr, size_limits=None, vr_rules=None):
    """True if a release is bigger than allowed for this kind of scene."""
    size = release.get("size") or 0
    if not size:
        return False
    if is_vr:
        cap = (vr_rules or DEFAULT_VR_RULES)["max_bytes"]
    else:
        limits = size_limits or DEFAULT_SIZE_LIMITS
        resolution = ((release.get("quality") or {}).get("quality") or {}).get("resolution")
        cap = limits.get(resolution, limits["default"])
    return bool(cap) and size > cap


def load_vr_rules(db):
    """The VR limits from settings (`vr_max_height`, `vr_max_file_gb`,
    `vr_target_file_gb`, see routers/settings.py's DEFAULTS)."""
    from routers.settings import get_setting_value
    def num(key, default):
        try:
            return float(get_setting_value(db, key, "") or default)
        except ValueError:
            return float(default)
    return {
        "max_height": int(num("vr_max_height", 3456)),
        "max_bytes": int(num("vr_max_file_gb", 15) * 10**9),
        "target_bytes": int(num("vr_target_file_gb", 10) * 10**9),
        "min_bytes": int(num("vr_min_file_gb", 1.5) * 10**9),
    }


def vr_height(title):
    """The VR frame height a release title advertises (0 = none)."""
    heights = [int(h) for h in _VR_HEIGHT_P_RE.findall(title or "")]
    heights += [_VR_K_HEIGHT[int(k)] for k in _VR_K_RE.findall(title or "")]
    return max(heights) if heights else 0


def vr_candidate_problem(title, size, rules=None):
    """None if a candidate for a VR scene is acceptable, else the reason:
    'vr_2d' (explicitly a 2D version, incl. "VR2Normal"), 'vr_no_format'
    (only a bare "VR" token, typically the studio name -- no projection/
    stereo/headset/VR-resolution marker), 'vr_res_too_high' (above the 6K
    playback limit; "6K" is sold as up to 6912x3456), 'vr_too_large' (above
    the per-scene size cap), 'vr_too_small' (tiny re-encodes)."""
    rules = {**DEFAULT_VR_RULES, **(rules or {})}
    title = title or ""
    if _VR_2D_RE.search(title):
        return "vr_2d"
    height = vr_height(title)
    strong = bool(_VR_MARKER_RE.search(title) or _VR_FORMAT_RE.search(title) or _VR_DEVICE_RE.search(title)
                  or (height >= 1600 and height not in _FLAT_HEIGHTS) or _VR_K_RE.search(title) and height >= 2560)
    if not strong:
        return "vr_no_format"
    if height and height > rules["max_height"]:
        return "vr_res_too_high"
    if size and size > rules["max_bytes"]:
        return "vr_too_large"
    if size and size < rules["min_bytes"]:
        return "vr_too_small"
    return None


def safe_info_url(url):
    """The indexer's own details page for a release (2026-09-26, "link to
    the indexer" for possible matches / search results), or None. Dropped
    if it would carry an API key -- those URLs must never reach the UI."""
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    lowered = url.lower()
    if "apikey=" in lowered or "api_key=" in lowered or "&r=" in lowered:
        return None
    return url


def normalize_prowlarr_release(raw):
    """Prowlarr's own `/api/v1/search` shape has NO `quality` object at
    all (confirmed live 2026-09-22, multiple real queries) -- unlike
    Whisparr's `/release`, which gives a structured
    `quality.quality.{resolution,source}`. Category tags are also
    unreliable for resolution (confirmed live: a genuine 2160p release
    was tagged "XXX/x264" by one indexer, not "XXX/UHD"). Resolution
    and VR are parsed from the release TITLE text instead -- the real-
    world usenet naming convention reliably embeds both (confirmed
    against several real titles, including genuine VR releases like
    "SpyFam...VR.Pervert...2160p..."). Produces the exact shape
    `_select_best()`/`grab_candidate()` already consume from Whisparr's
    own `/release`, so neither needs to know which service produced a
    given candidate. Exported (not `_`-prefixed) since
    routers/releases.py's manual-search endpoint reuses this directly
    for display, not just for `_select_best()`'s own filtering."""
    title = raw.get("title") or ""
    resolution = resolution_class(name_height(title))
    if resolution is None and _SD_RE.search(title):
        resolution = 480
    source = "vr" if (_VR_RE.search(title) or _VR_MARKER_RE.search(title)) else "web"
    return {
        "title": title,
        "indexerId": raw.get("indexerId"),
        "indexer": raw.get("indexer"),
        "age": raw.get("age"),
        "size": raw.get("size"),
        "protocol": raw.get("protocol") or "usenet",
        "downloadUrl": raw.get("downloadUrl"),
        "infoUrl": safe_info_url(raw.get("infoUrl")),
        "quality": {"quality": {"resolution": resolution, "source": source}},
        # Torrent results (Torznab, Prowlarr): seeders/peers/hash/magnet and the indexer's seed goals; None for usenet.
        "seeders": raw.get("seeders"), "peers": raw.get("peers") if raw.get("peers") is not None else raw.get("leechers"),
        "infoHash": raw.get("infoHash"), "magnetUrl": raw.get("magnetUrl"),
        "seedRatio": raw.get("seedRatio"), "seedTime": raw.get("seedTime"),
    }


def _select_best(releases, allowed_resolutions, indexer_priorities, scene_title, scene_date, performer_names,
                  require_vr=False, rejected_titles=(), vr_rules=None, allow_unknown_resolution=False,
                  taken_titles=(), size_limits=None, duration_s=None, torrent_cfg=None):
    """Lowest sort key wins: (quality rank, indexer priority, age,
    -size) -- see module docstring for how this maps to Whisparr's own
    comparer chain. A candidate whose resolution isn't in
    `allowed_resolutions` (this project's own quality_profile, best-
    first) is dropped outright, same effect as Whisparr's own
    QualityAllowedByProfileSpecification rejection but against our own
    profile instead of Whisparr's. A candidate whose own title doesn't
    plausibly match `scene_title` (see `_title_plausible()`/module
    docstring) is dropped BEFORE any of that -- Whisparr's search isn't
    reliably scoped to the right scene for every movie, confirmed live.

    `require_vr=True` (see module docstring's VR section) switches the
    filter entirely: only candidates whose `quality.source == "vr"`
    survive, and `allowed_resolutions` is not consulted at all -- there
    is no meaningful resolution ranking within Whisparr's own VR
    quality tier (always reported as 1080 regardless of the real
    video), so `rank` is fixed at 0 for every surviving candidate and
    the remaining tiebreakers (indexer priority, age, size) decide.

    `rejected_titles` (2026-09-23 -- real pre-existing bug fixed
    alongside the "Fix wrong download" feature): candidates whose
    `_review_identity()` matches an already-rejected/blocklisted title
    for this scene are dropped, same as `_select_review_candidate()`
    already does -- previously this function never consulted
    `review_rejected_titles` at all, so a title a human explicitly
    rejected (or one the new "Fix wrong download" action just
    blocklisted after a wrong grab) could still get auto-grabbed again
    on the very next search.

    `allow_unknown_resolution` (2026-09-26, user decision): a release name
    without any resolution ("Some.Series.With.Host.#1_3.Performer B" --
    the RIGHT scene, dropped as quality_not_allowed while a wrong one was
    grabbed) is accepted, ranked after every known allowed resolution; the
    file's real resolution is checked after the download (see
    file_importer's quality check). `taken_titles`: release names another
    scene already holds (release_repo.grabbed_titles_of_other_scenes())."""
    rejected = _split_rejected(rejected_titles)
    taken_identities = {_review_identity(t) for t in taken_titles}
    cfg = torrent_cfg or DEFAULT_TORRENT_CFG
    pool = []
    for r in releases:
        title = r.get("title")
        if _is_rejected(title, rejected, r) or _review_identity(title) in taken_identities:
            continue
        if predates_scene(scene_date, r):
            continue
        if not _candidate_plausible(scene_title, scene_date, performer_names, title):
            continue
        quality = (r.get("quality") or {}).get("quality") or {}
        size = r.get("size") or 0
        torrent = (r.get("protocol") or "usenet") != "usenet"
        tier, estimated = 0, False
        if require_vr:
            if quality.get("source") != "vr":
                continue
            # Stricter VR rules (2026-09-26), see vr_candidate_problem():
            # highest resolution up to the limit first, and between equals
            # the size closest to the target instead of "largest wins".
            if vr_candidate_problem(title, size, vr_rules):
                continue
            rank = -vr_height(title)
            size_key = abs(size - (vr_rules or DEFAULT_VR_RULES)["target_bytes"]) if size else float("inf")
        else:
            if too_large(r, False, size_limits):
                continue
            resolution = quality.get("resolution")
            codec = torrent_estimate.codec_of(title)
            # A name without a resolution: judged from the scene's length and the size (v0.231.0), ranked just after a named one.
            if resolution is None and duration_s and allowed_resolutions:
                guess = torrent_estimate.estimate_resolution(size, duration_s, codec)
                if guess in allowed_resolutions:
                    resolution, estimated = guess, True
            if resolution is None and allow_unknown_resolution and allowed_resolutions:
                rank = len(allowed_resolutions)
            elif resolution not in allowed_resolutions:
                continue
            else:
                rank = allowed_resolutions.index(resolution) + (0.5 if estimated else 0)
            size_key = -size
            # Last resort, used only when nothing better is left: a size that cannot be right for this length (mislabelled), or a torrent
            # nobody seeds.
            if duration_s and torrent_estimate.size_fit(size, duration_s, codec) is False:
                tier = 1
            if torrent and r.get("seeders") is not None and r["seeders"] < cfg["min_seeders"]:
                tier = 1
        pool.append({"r": r, "title": title, "rank": rank, "size_key": size_key, "tier": tier, "torrent": torrent,
                     "seeders": r.get("seeders") if r.get("seeders") is not None else cfg["min_seeders"]})

    # Torrents: the best resolution unless it has few seeders and a lower one has many more (the others keep their order behind it).
    if not require_vr:
        preferred = _preferred_torrent_rank([p for p in pool if p["tier"] == 0 and p["torrent"]], cfg)
        if preferred is not None:
            for p in pool:
                if p["torrent"] and p["tier"] == 0 and int(p["rank"]) != preferred:
                    p["rank"] += 100

    best, best_key = None, None
    for p in pool:
        r = p["r"]
        priority = indexer_priorities.get(r.get("indexerId"), 25)
        age = r.get("age")
        age = age if age is not None else float("inf")
        proto = 1 if p["torrent"] else 0  # "prefer usenet" like the *arr default delay profile: a tiebreak after quality
        if require_vr:
            key = (p["rank"], proto, priority, p["size_key"], age)
        else:
            key = (p["rank"], proto, -p["seeders"] if p["torrent"] else 0, priority, age, p["size_key"])
        key = (p["tier"], 0 if _names_distinguishing_performer(scene_title, performer_names, p["title"]) else 1) + key
        if best_key is None or key < best_key:
            best_key, best = key, r
    return best


def _preferred_torrent_rank(candidates, cfg):
    """The resolution rank (int) the torrents should be taken at, or None without torrents: the best one, unless it has fewer than
    `plenty` seeders and a lower one has at least `factor` times as many (then the lower one with the most seeders)."""
    groups = {}
    for p in candidates:
        g = int(p["rank"])
        groups[g] = max(groups.get(g, 0), p["seeders"])
    if not groups:
        return None
    best = min(groups)
    if groups[best] >= cfg["plenty"]:
        return best
    need = cfg["factor"] * max(groups[best], 1)
    better = [(seeds, -g) for g, seeds in groups.items() if g != best and seeds >= need]
    return -max(better)[1] if better else best


# Search log (2026-09-25, Indexers-tab stage 4): how many candidates to keep per search. Was 50 (v0.231.3: 500) -- the log is paged in the
# plugin, and the listings no longer carry it (release_repo._listed()), so it only costs room in the database (about 0.4 KB per result).
_SEARCH_LOG_MAX_CANDIDATES = 500
# Display order of verdicts in the log -- the interesting ones first.
_VERDICT_ORDER = ("grabbed", "review", "lower_rank", "quality_not_allowed", "not_vr",
                  "vr_2d", "vr_no_format", "vr_res_too_high", "vr_too_large", "vr_too_small",
                  "too_large", "title_mismatch", "too_old", "other_scene", "blocklisted")


def torrent_facts(r, duration_s=None):
    """What the search log and the manual-search list show beside a result (v0.231.0): seeders / peers of a torrent, the resolution judged from
    the length when the name has none, and why a size does not fit. Only the keys that apply."""
    out = {}
    if (r.get("protocol") or "usenet") != "usenet":
        out["protocol"] = "torrent"
        info_hash = release_hash(r)
        if info_hash:  # no link, no key: enough to add the torrent again as a magnet when the indexers do not answer (_grab_from_previous_log)
            out["info_hash"] = info_hash
        if r.get("seeders") is not None:
            out["seeders"] = r["seeders"]
        if r.get("peers") is not None:
            out["peers"] = r["peers"]
    named = name_height(r.get("title"))
    if named is not None and resolution_class(named) != named:
        out["named_height"] = named  # a name with 540p is taken for the 480p class; the list says what the name stated
    if duration_s:
        codec = torrent_estimate.codec_of(r.get("title"))
        quality = (r.get("quality") or {}).get("quality") or {}
        if quality.get("resolution") is None:
            guess = torrent_estimate.estimate_resolution(r.get("size") or 0, duration_s, codec)
            if guess:
                out["est_resolution"] = guess
        note = torrent_estimate.describe(r.get("size") or 0, duration_s, codec)
        if note:
            out["size_note"] = note
    return out


def _explain_candidates(releases, allowed_resolutions, scene_title, scene_date, performer_names,
                        require_vr, rejected_titles, chosen, review, vr_rules=None, allow_unknown_resolution=False,
                        taken_titles=(), size_limits=None, duration_s=None):
    """Per-candidate verdicts for the search log -- the SAME predicates
    `_select_best()` applies, in the same order, just recorded instead of
    silently `continue`d past, so the log shows exactly why each result
    was or wasn't used. Deliberately a separate function rather than a
    refactor of `_select_best()`: the grab decision stays untouched.
    `chosen` is the candidate `_select_best()` returned (same object,
    compared by identity); `review` is `_select_review_candidate()`'s
    result, a NEW dict, so it's matched by title + downloadUrl."""
    rejected = _split_rejected(rejected_titles)
    taken_identities = {_review_identity(t) for t in taken_titles}
    entries = []
    for r in releases:
        title = r.get("title")
        quality = (r.get("quality") or {}).get("quality") or {}
        if r is chosen:
            verdict = "grabbed"
        elif review is not None and (title, r.get("downloadUrl")) == (review.get("title"), review.get("downloadUrl")):
            verdict = "review"
        elif _is_rejected(title, rejected, r):
            verdict = "blocklisted"
        elif _review_identity(title) in taken_identities:
            verdict = "other_scene"
        elif predates_scene(scene_date, r):
            verdict = "too_old"
        elif not _candidate_plausible(scene_title, scene_date, performer_names, title):
            verdict = "title_mismatch"
        elif require_vr and quality.get("source") != "vr":
            verdict = "not_vr"
        elif require_vr and vr_candidate_problem(title, r.get("size"), vr_rules):
            verdict = vr_candidate_problem(title, r.get("size"), vr_rules)
        elif not require_vr and too_large(r, False, size_limits):
            verdict = "too_large"
        elif (not require_vr and quality.get("resolution") not in allowed_resolutions
              and not (quality.get("resolution") is None and allow_unknown_resolution and allowed_resolutions)):
            verdict = "quality_not_allowed"
        else:
            verdict = "lower_rank"
        entry = {
            "title": title, "indexer": r.get("indexer"), "resolution": quality.get("resolution"),
            "vr": quality.get("source") == "vr", "size": r.get("size"), "age": r.get("age"),
            "verdict": verdict,
            "info_url": safe_info_url(r.get("infoUrl")),
        }
        entry.update(torrent_facts(r, duration_s))
        entries.append(entry)
    entries.sort(key=lambda e: _VERDICT_ORDER.index(e["verdict"]))
    return entries[:_SEARCH_LOG_MAX_CANDIDATES]


# A search log older than this is not used as a source of torrents (seeders and availability change)
PREVIOUS_LOG_MAX_AGE_HOURS = 7 * 24


def _grab_from_previous_log(db, provider, scene_stashdb_id, previous, rejected_titles, torrent_cfg):
    """The indexers did not answer while a dead torrent is being replaced (v0.231.7): adds the best torrent of the LAST search instead, as a magnet
    link built from its info hash (the log keeps no download links: they carry the indexer's key; a magnet does not need the indexer at all).
    Candidates: torrents the last search found plausible (OK / possible match / what was grabbed), not given up on (hash blocklist, which
    already holds the dead torrent), not flagged for a wrong size, with the minimum seeders; most seeders first, then the log's own order.
    Returns the grab dict, or None when there is nothing to take (no hashes in an older log, too old, nothing left)."""
    try:
        # `kept_from`: a failed search keeps the candidates of an older one (its time), the log's own time is then the failed attempt's
        searched = datetime.strptime(str(previous.get("kept_from") or previous.get("searched_at") or "")[:19],
                                     "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if (datetime.now(timezone.utc) - searched).total_seconds() > PREVIOUS_LOG_MAX_AGE_HOURS * 3600:
        return None
    split = _split_rejected(rejected_titles)
    options = []
    for i, c in enumerate(previous.get("candidates") or []):
        info_hash = str(c.get("info_hash") or "").lower()
        if (c.get("protocol") != "torrent" or not info_hash or c.get("verdict") not in ("grabbed", "review", "lower_rank")
                or c.get("size_note") or _is_rejected(c.get("title"), split, {"infoHash": info_hash, "title": c.get("title")})):
            continue
        seeders = c.get("seeders")
        if seeders is None or seeders < torrent_cfg["min_seeders"]:
            continue
        options.append((-seeders, i, c, info_hash))
    if not options:
        return None
    _, _, c, info_hash = min(options)
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(str(c.get('title') or ''), safe='')}"
    candidate = {"title": c.get("title"), "indexer": c.get("indexer"), "protocol": "torrent", "size": c.get("size"), "age": c.get("age"),
                 "infoHash": info_hash, "magnetUrl": magnet, "downloadUrl": magnet, "seeders": c.get("seeders"), "peers": c.get("peers")}
    result = grab_candidate(db, provider, scene_stashdb_id, candidate, replace_job=True)
    if not result.get("grab"):
        return None
    history.scene("warning", "torrent", f"The indexers did not answer: took the best torrent of the last search instead: {c.get('title')}",
                  provider, scene_stashdb_id, details={"seeders": c.get("seeders"), "from": previous.get("kept_from") or previous.get("searched_at")})
    return result["grab"]


# The first marker of the "quality part" of a release name: "XXX", a resolution (720p, 1080p, 2160p), a codec (x264, x265, h264, h265, hevc).
# Everything from there on may differ between encodes of the SAME release (v0.174.0: "Anna.Placeholder.Title.Here.1080p" was blocklisted and its
# "...1080P.x265" twin was grabbed the next day because only an "XXX" marker was recognised).
_QUALITY_SUFFIX_RE = re.compile(r"\b(?:xxx|\d{3,4}p|x26[45]|h26[45]|hevc)\b")


# Two kinds of entries in a scene's blocklist (`review_rejected_titles`):
#  * a plain title -- the release as such is wrong (wrong scene, wrong content, rejected by the user): compared by `_review_identity()`, so its
#    other encodes (720p / 1080p / x265 ...) are blocked too;
#  * "exact:<title>" -- THIS FILE was unusable (below the quality profile, unreadable): only that exact release name is blocked. Found live
#    2026-10-10: a 720x380 "...REMASTERED.XXX.MP4-GRP" was rejected for its size and, with the identity cut at "XXX", blocked the 720p and 1080p
#    HEVC encodes of the same release too -- exactly the files that would have been right.
EXACT_PREFIX = "exact:"


def _exact_key(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


# Third and fourth kind (v0.231.0): "hash:<info hash>" / "link:<link without keys>" -- THIS torrent is unusable (dead, stalled, abandoned). Torrent
# names are not unique: another torrent of the same release name, with another hash, may be fine. They live in the same set as the exact keys
# (they cannot collide: an exact key has no ':').
HASH_PREFIX = "hash:"
LINK_PREFIX = "link:"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def release_hash(r):
    """The torrent's info hash (lower-case hex) from the result's infoHash or the btih of its magnet link; '' when not known."""
    h = str(r.get("infoHash") or "").strip().lower()
    if _HEX40_RE.match(h):
        return h
    for url in (r.get("magnetUrl"), r.get("downloadUrl")):
        if url and str(url).lower().startswith("magnet:"):
            m = re.search(r"xt=urn:btih:([0-9a-zA-Z]+)", str(url))
            if m and _HEX40_RE.match(m.group(1).lower()):
                return m.group(1).lower()
    return ""


def link_key(url):
    """A link without the keys in it (apikey=, passkey=...), so it can be compared and stored."""
    u = str(url or "").strip()
    if not u or u.lower().startswith("magnet:"):
        return ""
    base, _, query = u.partition("?")
    kept = [kv for kv in query.split("&") if kv and kv.split("=")[0].lower() not in ("apikey", "api_key", "passkey", "key", "token")]
    return (base + ("?" + "&".join(sorted(kept)) if kept else "")).lower()


def release_ids(r):
    """The blocklist entries ("hash:..", "link:..") that name this very torrent."""
    ids = set()
    h = release_hash(r)
    if h:
        ids.add(HASH_PREFIX + h)
    link = link_key(r.get("downloadUrl"))
    if link:
        ids.add(LINK_PREFIX + link)
    return ids


def _split_rejected(rejected_titles):
    """(identities, exact keys) of a scene's blocklist entries; the hash / link entries are among the exact keys, prefix kept."""
    identities, exacts = set(), set()
    for t in rejected_titles:
        t = str(t)
        if t.startswith(EXACT_PREFIX):
            exacts.add(_exact_key(t[len(EXACT_PREFIX):]))
        elif t.startswith(HASH_PREFIX):
            exacts.add(HASH_PREFIX + t[len(HASH_PREFIX):].strip().lower())
        elif t.startswith(LINK_PREFIX):
            exacts.add(LINK_PREFIX + t[len(LINK_PREFIX):].strip().lower())
        else:
            identities.add(_review_identity(t))
    return identities, exacts


def _is_rejected(title, split, release=None):
    """`release` (the result dict) lets a hash / link entry match; without it only the names count."""
    identities, exacts = split
    if release is not None and release_ids(release) & exacts:
        return True
    return _review_identity(title) in identities or _exact_key(title) in exacts


def _review_identity(candidate_title):
    """Normalized identity used to compare a candidate against the
    user's own past reject decisions (see `_select_review_candidate()`)
    -- a re-encode of the exact same release at a different resolution
    or from a different indexer (e.g. "...XXX.1080p.MP4-GRP" vs
    "...XXX.2160p.MP4-GRP", a real case seen live 2026-09-19) is still
    the SAME underlying release as far as a human's accept/reject
    decision goes; comparing raw titles let a rejected 1080p copy's
    2160p sibling immediately reappear as a "new" review candidate next
    cycle, which reads as broken, not as a second opinion worth asking
    for. Cuts the normalized title at its own "XXX" marker -- universal
    in this release-naming convention, always right before the
    resolution/format/release-group suffix that varies between encodes
    of the same release -- and keeps everything before it."""
    normalized = re.sub(r"[^a-z0-9]+", " ", (candidate_title or "").lower()).strip()
    m = _QUALITY_SUFFIX_RE.search(normalized)
    return normalized[:m.start()].strip() if m else normalized


def _select_review_candidate(releases, scene_title, scene_date, performer_names, rejected_titles,
                             is_vr=False, size_limits=None, vr_rules=None, allowed_resolutions=None, allow_unknown_resolution=False,
                             studio_name=None):
    """Finds the single best "needs review" candidate among `releases`
    -- one that names EVERY one of the movie's credited performers (see
    `_all_performers_plausible()`) but doesn't clear `_candidate_plausible()`'s
    bar for an automatic grab, and hasn't already been explicitly
    rejected by the user for this scene (`rejected_titles`, compared via
    `_review_identity()` so a different-resolution re-encode of an
    already-rejected release doesn't immediately reappear -- see
    release_repo.get_review_rejected_titles()). Deliberately NOT called
    unless `_select_best()` already came back empty -- an auto-
    acceptable candidate always wins outright, this only ever surfaces
    a second-best "ask a human" option. Ranked by age then size (largest
    first), same tiebreak spirit as `_select_best()`, since there's no
    meaningful quality-profile ranking to apply to a candidate that
    hasn't been confirmed as the right scene yet. The profile still sets the
    bounds (v0.205.1): a 2D release in a resolution the profile does not accept
    is not offered either -- "Accept" would grab something the profile forbids."""
    rejected = _split_rejected(rejected_titles)
    best, best_key = None, None
    for r in releases:
        title = r.get("title")
        if _is_rejected(title, rejected, r) or predates_scene(scene_date, r):
            continue
        if _candidate_plausible(scene_title, scene_date, performer_names, title):
            continue  # already auto-acceptable, not a "review" case
        near_date = _near_date_studio_match(scene_title, scene_date, performer_names, studio_name, title)
        if not near_date and not _all_performers_plausible(performer_names, title):
            continue
        if _year_contradicts(scene_title, scene_date, title):
            continue  # e.g. "Some_Performer_-_2021" for a 2026 scene
        if _PACK_RE.search(title or ""):
            continue  # site rips / packs / collections are never one scene
        # "Every performer named" is strong evidence with several performers,
        # but with ONE it matches any release of that person (2026-09-28:
        # other Some Performer / Other Performer scenes offered for unrelated ones)
        # -- then it also needs a date within 45 days or a word of the title.
        if not near_date and len(primary_names(performer_names)) < 2 and not _single_performer_evidence(
                scene_title, scene_date, performer_names, title):
            continue
        if too_large(r, is_vr, size_limits, vr_rules):
            continue  # e.g. a whole 147 GB site pack naming the performer
        if not is_vr and allowed_resolutions:
            resolution = ((r.get("quality") or {}).get("quality") or {}).get("resolution")
            if resolution not in allowed_resolutions and not (resolution is None and allow_unknown_resolution):
                continue  # e.g. a 2160p release while the profile accepts 1080p / 720p
        # A VR scene (or a VR upgrade) is only ever offered a VR release, exactly like the automatic choice demands
        # (_select_best(require_vr=True)). Found 2026-10-04: a flat 720p release with the same two performers was offered as the
        # "upgrade" of a VR scene -- the review fallback had skipped the VR test.
        if is_vr:
            quality = (r.get("quality") or {}).get("quality") or {}
            if quality.get("source") != "vr" or vr_candidate_problem(title, r.get("size"), vr_rules):
                continue
        age = r.get("age")
        age = age if age is not None else float("inf")
        size = r.get("size") or 0
        key = (age, -size)
        if best_key is None or key < best_key:
            best_key, best = key, r
    if best is None:
        return None
    return {
        "title": best.get("title"),
        "indexer": best.get("indexer"),
        "protocol": best.get("protocol") or "usenet",
        "downloadUrl": best.get("downloadUrl"),
        "infoUrl": safe_info_url(best.get("infoUrl")),
        "size": best.get("size"),
        "age": best.get("age"),
        **torrent_extras(best),
    }


# "Search visibility" (Milestone 3, 2026-09-19, user's own request
# right after the first real grab succeeded -- there's no Whisparr-
# style Activity page here to see what a search did/didn't find).
# release_repo.set_search_result_for_scene() records one of these after
# EVERY search_and_grab() attempt, not just a successful one, so the
# Releases page can show why nothing happened instead of a release just
# silently sitting in "Sent to Whisparr" forever. Plain strings (not an
# enum class) -- kept simple since the only other consumer is the
# frontend's own display-label lookup.
RESULT_NO_QUALITY_PROFILE = "no_quality_profile"
RESULT_VR_DISABLED = "vr_disabled"
RESULT_SEARCH_FAILED = "search_failed"
# The scene could not be checked at the provider (timeout etc.): no search this time, so a stale title / missing cast cannot pull in a wrong
# release (v0.176.0); tried again at the next run.
RESULT_PROVIDER_LOOKUP_FAILED = "provider_lookup_failed"
# The explicitly chosen search backend isn't usable (2026-09-25, see
# search_sources.py's UnavailableSource) -- e.g. 'prowlarr' selected
# but no Prowlarr URL saved.
RESULT_SEARCH_BACKEND_UNAVAILABLE = "search_backend_unavailable"
RESULT_NO_CANDIDATES = "no_candidates"
RESULT_TITLE_MISMATCH_ONLY = "title_mismatch_only"
RESULT_NO_QUALITY_MATCH = "no_quality_match"
RESULT_NEEDS_REVIEW = "needs_review"
RESULT_NO_DOWNLOAD_CLIENT = "no_download_client"
RESULT_CLIENT_NOT_IMPLEMENTED = "client_not_implemented"
RESULT_GRAB_FAILED = "grab_failed"
RESULT_GRABBED = "grabbed"
# A download for this scene is already running (2026-09-28, found live:
# the auto hand-off grabbed "Bendy Mistress" twice in one scan, once per
# watched studio entry). No second search or grab until it's done.
RESULT_ALREADY_DOWNLOADING = "already_downloading"
RESULT_DUPLICATE_DOWNLOAD = "duplicate_download"
RESULT_DAILY_CAP = "daily_grab_cap"


# Parent studios of a scene's studio (v0.103.0): a release of a sub-studio is often named after the PARENT ("Private.26.09.22..." for
# Anal Introductions), so a search under the sub-studio's own name finds nothing. The resolver (set by main.py, backed by Stash's own
# studio hierarchy and cached there) returns the parent names, nearest first.
_parent_resolver = None


def set_parent_resolver(fn):
    global _parent_resolver
    _parent_resolver = fn


def parent_studio_names(db, provider, scene_stashdb_id, studio_name):
    if _parent_resolver is None:
        return []
    try:
        studio_id, stored_name = release_repo.studio_of_scene(db, provider, scene_stashdb_id)
        return list(_parent_resolver(studio_id, studio_name or stored_name) or [])
    except Exception:
        logger.warning("indexer_search: parent studio lookup failed for %s scene %s", provider, scene_stashdb_id, exc_info=True)
        return []


# The SITE behind a scene (v0.230.0). StashDB often credits a performer-named creator as the studio ("Some Creator") while the release carries
# the site's name ("ExampleSite.Jane.Roe.Title.Words..."); the scene's "Studio" link (https://www.examplesite.com/watch/...) names it.
# `_site_resolver(provider, scene_id)` -> [{"url", "site"}] is set by main.py (cached in release.scene_urls, one provider request per scene).
_site_resolver = None


def set_site_resolver(fn):
    global _site_resolver
    _site_resolver = fn


def _brand_from_url(url):
    """'examplesite' from https://www.examplesite.com/watch/...; '' when the host has no usable name."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    labels = [x for x in host.split(".") if x and x != "www"]
    if len(labels) < 2:
        return ""
    name = labels[-3] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net") else labels[-2]
    return re.sub(r"[^a-z0-9]", "", name)


def studio_site_names(db, provider, scene_stashdb_id, *known_names):
    """Site names of the scene's "Studio" links that differ from the studio names we search anyway (`known_names`: the studio and its parents)."""
    if _site_resolver is None:
        return []
    try:
        urls = _site_resolver(provider, scene_stashdb_id) or []
    except Exception:
        logger.warning("indexer_search: site lookup failed for %s scene %s", provider, scene_stashdb_id, exc_info=True)
        return []
    known = {re.sub(r"[^a-z0-9]", "", str(n or "").lower()) for names in known_names for n in (names if isinstance(names, (list, tuple)) else [names])}
    out = []
    for u in urls:
        if (u.get("site") or "").strip().lower() != "studio":
            continue
        brand = _brand_from_url(u.get("url") or "")
        # not when it is the studio's own name or a part of it ("brazzers" for "Brazzers Exxtra"): the queries by studio name cover that
        if brand and brand not in out and not any(k and (brand in k or k in brand) for k in known):
            out.append(brand)
    return out


def own_studio_names(db, provider, scene_stashdb_id, studio_name):
    """Parent studios and site names of the scene (what the search queries use besides the studio itself)."""
    parents = parent_studio_names(db, provider, scene_stashdb_id, studio_name)
    return parents + studio_site_names(db, provider, scene_stashdb_id, studio_name, parents)


def _title_without_cast(scene_title, performer_names):
    """'Quiet student reads late' from 'Quiet student reads late - Jane Roe & John Doe': a title that
    ends in ' - <cast>' never appears like that in a release name, and the indexers AND every word of the query."""
    m = re.match(r"^(.*\S)\s+[-\u2013]\s+([^-\u2013]+)$", (scene_title or "").strip())
    if not m:
        return ""
    tail = m.group(2).lower()
    if any(str(n).lower() in tail for n in primary_names(performer_names) if n and len(str(n)) > 2):
        return m.group(1).strip()
    return ""


def _viable_lead(releases, scene_title, scene_date, performer_names, rejected_titles, taken_titles):
    """True when a result looks like this scene AND is not blocklisted / not already taken for another scene -- the only kind of lead that makes
    the next, wider query unnecessary. (A plausible but blocklisted name used to end the search at the first query: 0.217.1.)"""
    rejected = _split_rejected(rejected_titles)
    taken_identities = {_review_identity(t) for t in taken_titles}
    return any(_candidate_plausible(scene_title, scene_date, performer_names, r.get("title"))
               and not _is_rejected(r.get("title"), rejected, r) and _review_identity(r.get("title")) not in taken_identities
               for r in releases)


def _build_queries(scene_title, performer_names, studio_name, scene_date, parent_names=(), site_names=()):
    """Staged queries for a sidecar-side search (2026-09-25, user
    report: an I Love POV scene "Exes and Oral" got "no indexer results"
    while 7 correct releases existed). Many studios name releases
    `Studio.YY.MM.DD.Performer...` with NO scene-title words at all, so
    a title-only query finds nothing -- even though the plausibility
    check already accepts such names via date+performer. Order:
    1. the scene title (unchanged behavior, most specific when it works)
    2. the credited performers (first two -- indexer search ANDs words,
       and long casts are often truncated in release names)
    3. studio name without spaces/punctuation + date as YY.MM.DD
       ("ILovePOV 26.09.22" -- matches the dotted date in the name)
       (+ the same with the part of the name before a ":" subtitle, v0.217.1)
    4. the same with each PARENT studio's name (v0.103.0), see parent_studio_names()
    5. (v0.230.0) the title without a trailing " - <cast>" (right after the full title), and the SITE of the scene's "Studio" link with the
       first performer ("examplesite Jane Roe") and with the date -- see studio_site_names()
    search_and_grab() only moves on to the next query while nothing
    plausible has turned up, to keep indexer API hits (and their daily
    limits) low. Empty/duplicate queries are skipped."""
    queries = []
    if scene_title and scene_title.strip():
        queries.append(scene_title.strip())
        bare = _title_without_cast(scene_title, performer_names)
        if bare:
            queries.append(bare)
    names = [str(n) for n in primary_names(performer_names) if n and n.strip()]
    if names:
        queries.append(" ".join(names[:2]))
        for brand in site_names or ():
            queries.append(f"{brand} {names[0]}")
    # The studio's name as release names write it (v0.217.1): "Some Studio: The Series" is released as "FamilyStrokes.26.10.16...", so the part before
    # a ":" / " - " subtitle is tried as well (after the full name, only while nothing plausible turned up).
    studio_variants = [studio_name or ""]
    base = re.split(r"\s*(?::|\s-\s)\s*", studio_name or "", maxsplit=1)[0]
    if base and base != (studio_name or ""):
        studio_variants.append(base)
    if scene_date and re.match(r"\d{4}-\d{2}-\d{2}$", scene_date):
        for variant in [*studio_variants, *(site_names or ())]:
            studio_compact = re.sub(r"[^A-Za-z0-9]", "", variant)
            if studio_compact:
                queries.append(f"{studio_compact} {scene_date[2:4]}.{scene_date[5:7]}.{scene_date[8:10]}")
    if scene_date and re.match(r"\d{4}-\d{2}-\d{2}$", scene_date):
        for parent in parent_names or ():
            parent_compact = re.sub(r"[^A-Za-z0-9]", "", parent or "")
            if parent_compact:
                queries.append(f"{parent_compact} {scene_date[2:4]}.{scene_date[5:7]}.{scene_date[8:10]}")
    # Studios often date a release a day off from the provider (v0.218.3): the day before / after, studio name only, tried last.
    if scene_date and re.match(r"\d{4}-\d{2}-\d{2}$", scene_date):
        from datetime import date, timedelta
        base_day = date(int(scene_date[:4]), int(scene_date[5:7]), int(scene_date[8:10]))
        first = re.sub(r"[^A-Za-z0-9]", "", studio_name or "")
        for offset in (-1, 1):
            neighbour = base_day + timedelta(days=offset)
            if first:
                queries.append(f"{first} {neighbour:%y.%m.%d}")
    seen, unique = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            unique.append(q)
    return unique


def search_and_grab(db, whisparr_client, movie_id, provider, scene_stashdb_id, scene_title, scene_date,
                     performer_names, quality_profile, is_vr=False, vr_enabled=False, search_source=None,
                     studio_name=None, candidate_filter=None, dry_run=False, allow_unknown_resolution=None,
                     exclude_hashes=(), replace_job=False, duration_s=None):
    """Full Milestone 3 flow for one release. Always returns
    `{"result": RESULT_*, "grab": grab_dict_or_None}` -- `grab` is only
    ever non-None when `result == RESULT_GRABBED`. Changed 2026-09-19
    (v0.41.2) from "grab dict or None" so a caller (routers/releases.py's
    handoff()) can hand the exact RESULT_* code straight back to the
    frontend, letting it patch a release row's status label in place
    instead of needing a full page/list refresh (and losing open
    <details> state) just to see an updated "not searched yet" ->
    "results found, none matched the title" message -- see
    entity-panel.js's confirmHandoff(). `scene_title` is this release's
    own clean title (release_repo's `title` column) -- required, used
    to reject candidates whose own release name doesn't plausibly match
    it (see module docstring's title-plausibility section). `scene_date`
    (release_repo's `scene_date` column, `YYYY-MM-DD` or `None`) and
    `performer_names` (Whisparr's own `movie["performerNames"]` -- the
    caller already has the movie object, no extra fetch needed) back
    `_candidate_plausible()`'s date+performer fallback for studios whose
    release names carry no scene-title wording at all (see
    `_performer_plausible()`'s docstring for why BOTH together, not
    date alone). Every call
    records the same `RESULT_*` outcome on the release row via
    release_repo.set_search_result_for_scene() -- see "search
    visibility" note above -- whether it finds something or not.

    Two independent gates, fixed 2026-09-19 (v0.40.2) after the
    original design (a VR release used a second `quality_profile`,
    same shape as the normal one) turned out to actively mislead: a
    VR-flagged release is matched purely by Whisparr's own `source:
    "vr"` quality tag (see module docstring), so there is nothing
    profile/resolution-shaped to configure for it.
    - `is_vr=False`: needs a real `quality_profile` dict (routers/
      settings.py's resolve_quality_profiles()) with 'resolutions'
      (ordered, best first); `None` means "not configured yet", a
      no-op.
    - `is_vr=True`: `quality_profile` is ignored entirely (not even
      read -- see `_select_best()`'s `require_vr` branch); gated
      instead by the plain `vr_enabled` bool (the `vr_search_enabled`
      setting).

    `search_source` (2026-09-25, was `prowlarr_client` since
    2026-09-22): the explicitly chosen search backend, see
    search_sources.py -- `None` means Whisparr's own
    `/release?movieId=X`; a source object fully REPLACES that as the
    candidate source for this attempt -- not merged. Deliberately simple: a live
    investigation found Whisparr's own search-term construction is the
    likely root cause of this project's whole title-mismatch bug class
    (querying Prowlarr directly with the exact case that motivated the
    title/date/performer-plausibility hardening returned 0 results, not
    Whisparr's own 53 WRONG ones), so merging Whisparr's noisier result
    set back in would undercut the whole point. `movie_id` is still
    required either way -- it's this project's own Whisparr bookkeeping
    record regardless of who searches for the file."""
    # `exclude_hashes` / `replace_job` (v0.231.0, torrent health): switching away from a dead torrent -- the old one is treated as blocklisted
    # for this search and the running download of the scene does not stop the grab (the caller removes the old torrent afterwards).
    # `duration_s`: the scene's length when the caller knows it; else it is asked from the provider (needed for the size estimates).
    # `candidate_filter(release) -> bool` / `dry_run` (2026-09-26, upgrade
    # search): the filter drops candidates that aren't an improvement over
    # the file already owned; a dry run searches and selects but writes
    # nothing and grabs nothing -- the result carries `would_grab` and the
    # candidate verdicts instead.
    dry_info = {}

    def _finish(result, grab=None):
        if dry_run:
            return {"result": result, "grab": None, **dry_info}
        release_repo.set_search_result_for_scene(db, provider, scene_stashdb_id, result)
        return {"result": result, "grab": grab}

    # Search log (2026-09-25) -- what was searched where, recorded on
    # every attempt that got as far as choosing a backend, see
    # _explain_candidates(). Whisparr builds its own query internally
    # from the movie, so there's no query text to show for it.
    backend = search_source.name if search_source is not None else "whisparr"
    # Each query actually sent: {query, results, error} -- see
    # _build_queries(). Stays empty for Whisparr (its own internal query).
    queries_sent = []

    def _log(candidates=None, error=None):
        if dry_run:
            dry_info.update(queries=list(queries_sent), candidates=candidates or [], error=error)
            return
        release_repo.set_search_details_for_scene(db, provider, scene_stashdb_id, {
            "backend": backend, "queries": queries_sent, "error": error,
            "total": len(releases) if releases is not None else 0,
            "candidates": candidates or [],
        })

    releases = None

    if not dry_run and not replace_job and release_repo.scene_has_active_download(db, provider, scene_stashdb_id):
        logger.info("indexer_search: %s scene %s already has a running download, not searching again",
                    provider, scene_stashdb_id)
        return _finish(RESULT_ALREADY_DOWNLOADING)

    if is_vr:
        if not vr_enabled:
            logger.warning("indexer_search: VR search not enabled, skipping VR movie %s", movie_id)
            return _finish(RESULT_VR_DISABLED)
    elif quality_profile is None:
        logger.warning("indexer_search: no quality profile configured, skipping movie %s", movie_id)
        return _finish(RESULT_NO_QUALITY_PROFILE)

    # Read before the search (v0.217.1): the widening below must not stop at a lead that is blocklisted or belongs to another scene.
    rejected_titles = list(release_repo.get_review_rejected_titles(db, provider, scene_stashdb_id)) + [
        HASH_PREFIX + str(h).lower() for h in exclude_hashes or ()]
    taken_titles = list(release_repo.grabbed_titles_of_other_scenes(db, provider, scene_stashdb_id))

    try:
        if search_source is not None:
            if not search_source.available:
                logger.warning("indexer_search: search backend %r unavailable (%s), skipping movie %s",
                               search_source.name, search_source.reason, movie_id)
                _log(error=search_source.reason)
                return _finish(RESULT_SEARCH_BACKEND_UNAVAILABLE)
            releases = []
            seen = set()
            parents = parent_studio_names(db, provider, scene_stashdb_id, studio_name)
            for q in _build_queries(scene_title, performer_names, studio_name, scene_date, parents,
                                    studio_site_names(db, provider, scene_stashdb_id, studio_name, parents)):
                try:
                    batch = search_source.search(q)
                except Exception as e:
                    logger.warning("indexer_search: query %r failed for movie %s: %s", q, movie_id, e)
                    queries_sent.append({"query": q, "results": None, "error": str(e)})
                    continue
                queries_sent.append({"query": q, "results": len(batch), "error": None})
                for r in batch:
                    key = (r.get("title"), r.get("downloadUrl"))
                    if key not in seen:
                        seen.add(key)
                        releases.append(r)
                if _viable_lead(releases, scene_title, scene_date, performer_names, rejected_titles, taken_titles):
                    break  # something that could really be taken found, no need to widen the search
            if queries_sent and all(q["error"] for q in queries_sent):
                raise RuntimeError("; ".join(q["error"] for q in queries_sent))
            priorities = search_source.indexer_priorities()
        elif whisparr_client is None or movie_id is None:
            # Whisparr-free release (2026-09-25, handoff.send_to_sidecar())
            # while search_backend is 'whisparr': Whisparr can only search
            # movies it tracks, and this one it doesn't.
            logger.warning("indexer_search: search backend is Whisparr, but %s scene %s isn't tracked there",
                           provider, scene_stashdb_id)
            _log(error="Whisparr can only search releases it tracks -- choose Prowlarr or direct indexers "
                       "as search backend (Settings > Indexers)")
            return _finish(RESULT_SEARCH_BACKEND_UNAVAILABLE)
        else:
            releases = whisparr_client.get_release(movie_id)
            priorities = _indexer_priorities(whisparr_client)
    except Exception as e:
        logger.exception("indexer_search: release search failed for movie %s", movie_id)
        releases = None
        previous = None if dry_run else release_repo.get_search_details_for_scene(db, provider, scene_stashdb_id)
        # Switching away from a dead torrent while the indexers do not answer: the best torrent of the last search is taken instead
        if replace_job and previous and not dry_run:
            grabbed = _grab_from_previous_log(db, provider, scene_stashdb_id, previous, rejected_titles, load_torrent_settings(db))
            if grabbed is not None:
                return _finish(RESULT_GRABBED, grabbed)
        _log(error=str(e))
        if previous and previous.get("candidates") and not dry_run:
            # the failed search does not wipe what the last one found: "Last" keeps showing it (marked with its time)
            release_repo.set_search_details_for_scene(db, provider, scene_stashdb_id, {
                "backend": backend, "queries": queries_sent, "error": str(e), "total": previous.get("total", 0),
                "candidates": previous["candidates"], "kept_from": previous.get("kept_from") or previous.get("searched_at")})
        return _finish(RESULT_SEARCH_FAILED)

    if candidate_filter is None:
        # A scene that is being upgraded (release.upgrade_of, e.g. after "To wishlist" on Upgrade wanted) is held to the upgrade rules by every
        # search, also the regular ones: only a release above the library file, no last-resort kinds (v0.237.1).
        import upgrades  # lazy: upgrades imports this module
        candidate_filter = upgrades.filter_for_scene(db, provider, scene_stashdb_id)
    if releases and candidate_filter is not None:
        releases = [r for r in releases if candidate_filter(r)]
    if not releases:
        _log()
        return _finish(RESULT_NO_CANDIDATES)
    # Release names the stash-box shows as ANOTHER scene (studio + date of the name, performer in its cast; v0.259.0) are never this scene's
    taken_titles += other_scene_check.titles_of_other_scenes(provider, scene_stashdb_id, scene_date, performer_names, studio_name, releases,
                                                        own_studio_names(db, provider, scene_stashdb_id, studio_name))

    allowed_resolutions = quality_profile["resolutions"] if quality_profile else []
    # Fetched once, up front, and used both here and in the review-
    # candidate fallback below (2026-09-23 -- previously only fetched
    # for the fallback, so _select_best() never excluded an already-
    # rejected/blocklisted title from the AUTOMATIC grab path at all;
    # see _select_best()'s own docstring for the real bug this fixes).
    vr_rules = load_vr_rules(db) if is_vr else None
    # Unknown resolution: normal searches only -- an upgrade must PROVE it's
    # better, which a name without a resolution can't (2026-09-26).
    if allow_unknown_resolution is None:
        allow_unknown_resolution = candidate_filter is None
    size_limits = load_size_limits(db)
    torrent_cfg = load_torrent_settings(db)
    if duration_s is None and not is_vr and any((r.get("protocol") or "usenet") != "usenet" or
                                                 ((r.get("quality") or {}).get("quality") or {}).get("resolution") is None
                                                 for r in releases):
        duration_s = scene_duration(provider, scene_stashdb_id)  # only asked when it can matter: a torrent, or a name without a resolution
    best = _select_best(releases, allowed_resolutions, priorities, scene_title, scene_date, performer_names,
                         require_vr=is_vr, rejected_titles=rejected_titles, vr_rules=vr_rules,
                         allow_unknown_resolution=allow_unknown_resolution, taken_titles=taken_titles,
                         size_limits=size_limits, duration_s=duration_s, torrent_cfg=torrent_cfg)
    if best is None and not is_vr and candidate_filter is None:
        # Last resort for an older scene (v0.218.0): 2160p (scaled down to 1080p at the import) or SD, after everything in the profile.
        extra = [r for r in fallback_resolutions(db, scene_date) if r not in allowed_resolutions]
        if extra:
            wider = list(allowed_resolutions) + extra
            best = _select_best(releases, wider, priorities, scene_title, scene_date, performer_names,
                                 require_vr=is_vr, rejected_titles=rejected_titles, vr_rules=vr_rules,
                                 allow_unknown_resolution=allow_unknown_resolution, taken_titles=taken_titles,
                                 size_limits=size_limits, duration_s=duration_s, torrent_cfg=torrent_cfg)
            if best is not None:
                allowed_resolutions = wider
    if best is None:
        # RESULT_NEEDS_REVIEW (2026-09-19): a candidate naming every one
        # of the movie's credited performers exists, but didn't clear
        # the bar for an automatic grab (e.g. date off by more than a
        # day -- see _all_performers_plausible()'s docstring for the
        # real near-miss this was built from). Surfaced for a human
        # decision instead of either auto-grabbing it (too risky, see
        # v0.41.7) or silently filing it next to a genuine non-match.
        # An older scene may be offered a 2160p / SD release for review too (the last-resort kinds, see fallback_resolutions()).
        review_resolutions = list(allowed_resolutions)
        if not is_vr and candidate_filter is None:
            review_resolutions += [r for r in fallback_resolutions(db, scene_date) if r not in review_resolutions]
        review_candidate = _select_review_candidate(releases, scene_title, scene_date, performer_names,
                                                      list(rejected_titles) + taken_titles, is_vr=is_vr,
                                                      size_limits=size_limits, vr_rules=vr_rules,
                                                      allowed_resolutions=review_resolutions,
                                                      allow_unknown_resolution=allow_unknown_resolution,
                                                      studio_name=studio_name)
        _log(_explain_candidates(releases, review_resolutions, scene_title, scene_date, performer_names,
                                 is_vr, rejected_titles, None, review_candidate, vr_rules,
                                 allow_unknown_resolution, taken_titles, size_limits, duration_s))
        if not dry_run:
            previous = release_repo.get_review_candidate_for_scene(db, provider, scene_stashdb_id)
            release_repo.set_review_candidate_for_scene(db, provider, scene_stashdb_id, review_candidate)
            # Logged only when it CHANGES -- the periodic search re-finds
            # the same candidate every run.
            # Upgrade searches (candidate_filter set) log their own, see
            # upgrades.search_one() -- their rows are recreated each run.
            if (review_candidate and candidate_filter is None
                    and (previous or {}).get("title") != review_candidate.get("title")):
                history.scene("info", "review", f"Possible match needs review: {review_candidate.get('title')}",
                              provider, scene_stashdb_id)
        if review_candidate is not None:
            return _finish(RESULT_NEEDS_REVIEW)
        # Distinguish "nothing even looked like this scene" from
        # "something looked right but the quality/VR tag didn't match"
        # -- both are None from _select_best(), but they mean very
        # different things to a user checking why nothing was grabbed.
        any_title_match = any(_candidate_plausible(scene_title, scene_date, performer_names, r.get("title")) and not predates_scene(scene_date, r)
                               for r in releases)
        return _finish(RESULT_NO_QUALITY_MATCH if any_title_match else RESULT_TITLE_MISMATCH_ONLY)

    _log(_explain_candidates(releases, allowed_resolutions, scene_title, scene_date, performer_names,
                             is_vr, rejected_titles, best, None, vr_rules, allow_unknown_resolution, taken_titles,
                             size_limits, duration_s))
    if dry_run:
        dry_info["would_grab"] = {"title": best.get("title"), "indexer": best.get("indexer"),
                                  "size": best.get("size"), "info_url": safe_info_url(best.get("infoUrl"))}
        return _finish("dry_run")
    return grab_candidate(db, provider, scene_stashdb_id, {
        "title": best.get("title"), "indexer": best.get("indexer"), "size": best.get("size"),
        "protocol": best.get("protocol") or "usenet", "downloadUrl": best.get("downloadUrl"), "age": best.get("age"),
        **torrent_extras(best),
    }, replace_job=replace_job)


def _norm_release(name):
    """Release names differ in punctuation between the indexer and SABnzbd ("," becomes "_") -- compare
    letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _duplicate_guard(client, title, db=None):
    """Why this release must NOT be sent to SABnzbd again (or None). 2026-09-29: an error made the same five
    releases be fetched ~21 times each (97 NZB downloads, the indexer complained). SABnzbd's own duplicate
    detection only sees the NZB AFTER the indexer served it, so the check has to happen before the call.
    Queued or already downloaded -> never again; failed twice -> not a third time. Fails closed."""
    key = _norm_release(title)
    if not key:
        return None
    try:
        queued = client.get_queue()
        history_rows = client.get_history(limit=1000)
    except Exception:
        logger.exception("indexer_search: couldn't check SABnzbd for duplicates of %r", title)
        return "couldn't check SABnzbd's queue/history for duplicates"
    if any(_norm_release(s.get("filename")) == key for s in queued):
        return "the same release is already in the SABnzbd queue"
    failed = 0
    for s in history_rows:
        if _norm_release(s.get("name")) == key:
            if (s.get("status") or "").lower() == "failed":
                failed += 1
            elif db is not None and download_history_cleanup.rejected_only(db, title):
                continue  # rejected for ITS scene (that scene's blocklist applies); another scene may be the right one for it (v0.218.1)
            else:
                return f"the same release is already in SABnzbd's history ({s.get('status')})"
    if failed >= 2:
        return f"the same release already failed {failed} times"
    if db is not None and download_history_cleanup.ledger_has(db, title):
        return "the same release was downloaded before (its entry was removed from the client's history after the import)"
    return None


# Torrent results carry more than a usenet one; these keys travel with a candidate from the search to the grab.
TORRENT_KEYS = ("magnetUrl", "infoHash", "seeders", "peers", "seedRatio", "seedTime")
RECENT_DAYS = 14  # *arr: a release younger than this gets the client's "recent" priority


def torrent_extras(r):
    return {k: r.get(k) for k in TORRENT_KEYS if r.get(k) is not None}


def candidate_url(c):
    """What gets sent to the client: the indexer's own link (NZB / .torrent), else a magnet link."""
    return c.get("downloadUrl") or c.get("magnetUrl")


def _torrent_duplicate_guard(client, candidate):
    """Why this torrent must NOT be added again (or None): qBittorrent already has the very same torrent (hash). The release NAME no longer counts
    (v0.231.0): torrent names are not unique, a dead and a healthy torrent can share one. A link without a known hash is decided by
    QbittorrentClient.add(), which works out the hash of the file it fetches."""
    info_hash = release_hash(candidate)
    if not info_hash:
        return None
    try:
        torrents = client.get_torrents(category="")
    except Exception:
        logger.exception("indexer_search: couldn't check qBittorrent for duplicates of %r", candidate.get("title"))
        return "couldn't check qBittorrent for duplicates"
    for t in torrents:
        if (t.get("hash") or "").lower() == info_hash:
            return "qBittorrent already has this torrent"
    return None


def _grab_budget(db):
    """(day, grabs_today, limit): the daily cap on grabs of ANY kind (setting max_grabs_per_day, default 30)."""
    import datetime
    today = datetime.date.today().isoformat()
    with db.connect() as conn:
        limit_row = conn.execute("SELECT value FROM setting WHERE key = 'max_grabs_per_day'").fetchone()
        count_row = conn.execute("SELECT value FROM setting WHERE key = 'grab_count_day'").fetchone()
    try:
        limit = int(float((limit_row["value"] if limit_row else "") or 30))
    except ValueError:
        limit = 30
    day, _, n = ((count_row["value"] if count_row else "") or "").partition(":")
    return today, (int(n) if day == today and n.isdigit() else 0), limit


def _grab_counted(db):
    today, n, _ = _grab_budget(db)
    with db.connect() as conn:
        conn.execute("""INSERT INTO setting (key, value) VALUES ('grab_count_day', ?)
                        ON CONFLICT (key) DO UPDATE SET value = excluded.value""", (f"{today}:{n + 1}",))


def send_to_download_client(db, candidate, warn):
    """The shared half of every grab (scene or film): picks the download client, runs the duplicate guard and the daily cap, adds the
    NZB. `warn(message)` records a skipped grab for the caller's own History. Returns {"result": RESULT_*, "client_row", "nzo_id"}
    -- RESULT_GRABBED when the job was added."""
    protocol = candidate.get("protocol") or "usenet"
    client_row = download_client_repo.pick_for_protocol(db, protocol)
    if client_row is None:
        logger.warning("indexer_search: no enabled download client configured for protocol %r", protocol)
        return {"result": RESULT_NO_DOWNLOAD_CLIENT}
    if client_row["type"] == "qbittorrent":
        return _send_to_qbittorrent(db, candidate, client_row, warn)
    if client_row["type"] != "sabnzbd":
        logger.warning("indexer_search: download client type %r not implemented yet", client_row["type"])
        return {"result": RESULT_CLIENT_NOT_IMPLEMENTED}

    client = SabnzbdClient(client_row["url"], client_row["api_key"])
    reason = _duplicate_guard(client, candidate.get("title"), db)
    if reason:
        logger.warning("indexer_search: not grabbing %r -- %s", candidate.get("title"), reason)
        warn(f"Grab skipped: {reason}")
        return {"result": RESULT_DUPLICATE_DOWNLOAD}
    today, used, cap = _grab_budget(db)
    if used >= cap:
        logger.warning("indexer_search: daily grab cap reached (%s/%s) -- not grabbing %r", used, cap, candidate.get("title"))
        warn(f"Grab skipped: daily grab cap reached ({used}/{cap}; setting max_grabs_per_day)")
        return {"result": RESULT_DAILY_CAP}
    nzo_ids = client.add_url(candidate["downloadUrl"], category=client_row["category"] or None)
    _grab_counted(db)
    nzo_id = nzo_ids[0] if nzo_ids else None
    if not nzo_id:
        logger.error("indexer_search: SABnzbd accepted the add but returned no nzo_id for %r", candidate.get("title"))
        return {"result": RESULT_GRAB_FAILED}
    return {"result": RESULT_GRABBED, "client_row": client_row, "nzo_id": nzo_id}


def _send_to_qbittorrent(db, candidate, client_row, warn):
    """The torrent half of send_to_download_client(): same duplicate guard + daily cap, then the torrent goes to qBittorrent (the sidecar fetches the
    .torrent itself, so the indexer's key never reaches qBittorrent); the indexer's seed goals become per-torrent share limits (else the client's
    own). `nzo_id` is the torrent's info hash, the job id every caller stores."""
    client = QbittorrentClient(client_row["url"], client_row["username"], client_row["password"], client_row["category"], client_row.get("options"))
    reason = _torrent_duplicate_guard(client, candidate)
    if reason:
        logger.warning("indexer_search: not grabbing %r -- %s", candidate.get("title"), reason)
        warn(f"Grab skipped: {reason}")
        return {"result": RESULT_DUPLICATE_DOWNLOAD}
    today, used, cap = _grab_budget(db)
    if used >= cap:
        logger.warning("indexer_search: daily grab cap reached (%s/%s) -- not grabbing %r", used, cap, candidate.get("title"))
        warn(f"Grab skipped: daily grab cap reached ({used}/{cap}; setting max_grabs_per_day)")
        return {"result": RESULT_DAILY_CAP}
    url = candidate_url(candidate)
    age = candidate.get("age")
    try:
        info_hash = client.add(url, name=candidate.get("title"), recent=age is None or age <= RECENT_DAYS)
        if candidate.get("seedRatio") is not None or candidate.get("seedTime") is not None:
            client.set_share_limits(info_hash, ratio=candidate.get("seedRatio"), seed_minutes=candidate.get("seedTime"))
    except AlreadyAdded:
        logger.warning("indexer_search: not grabbing %r -- qBittorrent already has this torrent", candidate.get("title"))
        warn("Grab skipped: qBittorrent already has this torrent")
        return {"result": RESULT_DUPLICATE_DOWNLOAD}
    except QbittorrentError as e:
        logger.error("indexer_search: qBittorrent grab of %r failed: %s", candidate.get("title"), e)
        warn(f"Grab failed: {e}")
        return {"result": RESULT_GRAB_FAILED}
    _grab_counted(db)
    return {"result": RESULT_GRABBED, "client_row": client_row, "nzo_id": info_hash}


def grab_candidate(db, provider, scene_stashdb_id, candidate, replace_job=False):
    """Sends `candidate` (a dict with at least title/protocol/downloadUrl/
    indexer -- the same shape `_select_best()`/`_select_review_candidate()`
    produce) to this scene's configured download client and records the
    resulting job. Returns `{"result": RESULT_*, "grab": grab_dict_or_None}`,
    same shape as search_and_grab() itself. Factored out 2026-09-19 so
    the "accept this review candidate" action (routers/releases.py's
    accept_review_candidate()) shares the exact same grab path a normal
    auto-accepted candidate uses, instead of a second, drifting copy.
    `replace_job` (v0.231.0): the scene's running download is being replaced (a dead torrent), so it does not stop the grab; the caller
    removes the old download."""
    def _finish(result, grab=None):
        release_repo.set_search_result_for_scene(db, provider, scene_stashdb_id, result)
        return {"result": result, "grab": grab}

    # Last line of defence for every grab path (auto, Search now, accepted
    # review candidate, manual pick, upgrade): one download per scene.
    if not replace_job and release_repo.scene_has_active_download(db, provider, scene_stashdb_id):
        logger.warning("indexer_search: not grabbing %r -- %s scene %s already has a running download",
                       candidate.get("title"), provider, scene_stashdb_id)
        return _finish(RESULT_ALREADY_DOWNLOADING)

    sent = send_to_download_client(
        db, candidate, lambda msg: history.scene("warning", "grab", msg, provider, scene_stashdb_id,
                                                 details={"release": candidate.get("title")}))
    if sent["result"] != RESULT_GRABBED:
        return _finish(sent["result"])
    client_row, nzo_id = sent["client_row"], sent["nzo_id"]

    release_repo.set_download_job_for_scene(db, provider, scene_stashdb_id, client_row["id"], nzo_id)
    release_repo.set_grabbed_for_scene(db, provider, scene_stashdb_id, candidate.get("title"))
    release_repo.set_review_candidate_for_scene(db, provider, scene_stashdb_id, None)
    logger.info("indexer_search: grabbed %r from %s -> %s job %s",
                candidate.get("title"), candidate.get("indexer"), client_row["name"], nzo_id)
    size = candidate.get("size")
    history.scene("info", "grab", f"Grabbed {candidate.get('title')}", provider, scene_stashdb_id, details={
        "indexer": candidate.get("indexer"), "download_client": client_row["name"], "job": nzo_id,
        **({"size_gb": round(size / 1e9, 2)} if size else {})})
    return _finish(RESULT_GRABBED,
                   {"title": candidate.get("title"), "indexer": candidate.get("indexer"), "nzo_id": nzo_id})
