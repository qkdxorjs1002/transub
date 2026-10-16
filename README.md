<div align="center">
  <h1>Transub</h1>
  <p>Translate embedded subtitles into your chosen language, with context and formatting intact.</p>
  <p>
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square">
    <img alt="macOS and Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-555555?style=flat-square">
    <img alt="Powered by Codex CLI" src="https://img.shields.io/badge/translation-Codex%20CLI-111827?style=flat-square">
  </p>
  <p><strong>English</strong> · <a href="README.ko.md">한국어</a></p>
</div>

Transub researches a title's background, translates its embedded text subtitles through your ChatGPT login in Codex CLI, and adds an ASS track in your chosen language to the video. Korean is the default; select another supported language with `--target-language CODE`. It uses the Python standard library and FFmpeg; no OpenAI API key or third-party Python packages are required.

> [!IMPORTANT]
> **By default, Transub replaces the original MKV after verification. It does not create a backup.**
> To keep the original, process a single file with `-o '/path/to/output.mkv'`.
> Folder input does not accept `-o`.
> Allow roughly each original file's size in temporary space. Folder jobs can remux several files at once, so plan for the combined size of up to `--jobs` source files being processed concurrently.

[Quick start](#quick-start) · [Usage](#usage) · [Options](#options) · [Resume](#resume-and-output) · [Troubleshooting](#troubleshooting)

## Features

- **Context-aware translation:** researches names, relationships, terminology, and speech styles with Codex web search before translating.
- **ASS formatting preservation:** retains timing, styles, layers, tags, drawings, and overlapping events while replacing dialogue text.
- **Verified remuxing:** preserves existing video, audio, subtitles, chapters, and attachments; checks the result before replacing the source.
- **Resumable batches:** saves research and validated translations locally for reuse after an interruption.
- **Configurable target language:** supports 36 language codes, with Korean as the default and matching subtitle track metadata.
- **Parallel folder processing:** snapshots MKVs recursively and processes two files at a time by default, while keeping final output in relative-path order.

New jobs explicitly use **`gpt-6.1-sol` with `medium` reasoning** for both research and translation. Transub does not automatically fall back to another model.

## Quick start

1. Install **Python 3.10+**, **FFmpeg/FFprobe**, and a compatible [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) on macOS or Linux.
   On macOS with Homebrew:

   ```bash
   brew install ffmpeg
   ```

   On Linux, install FFmpeg and FFprobe with your distribution's package manager.

2. Sign in to Codex with your **ChatGPT account**, using file-based credential storage:

   ```bash
   codex login -c 'cli_auth_credentials_store="file"'
   ```

   Transub reads `auth.json` from `CODEX_HOME` (normally `~/.codex`). It cannot copy a keychain-only login. See the official [authentication guide](https://learn.chatgpt.com/docs/auth).

3. From the project directory, run:

   ```bash
   python3 transub.py '/path/to/video.mkv'
   ```

After completion, reopen the video and select **한국어 (Codex 번역)** in your player's subtitle menu. Add `--default-subtitle` (also available as `--default-korean`) to make the new track the default.

Codex CLI **0.153.4** was previously verified. Compatibility depends on the required isolation features, not just the version number; see [Privacy and verification](#privacy-and-verification). Network access and available Codex account usage are required.

## Usage

**Inspect subtitle tracks without translating or modifying the video:**

```bash
python3 transub.py '/path/to/video.mkv' --list-tracks
python3 transub.py '/path/to/videos' --list-tracks
```

Use the reported FFprobe stream index with `--source-track` to choose a subtitle track explicitly. Folder listing uses the same `--jobs` limit as translation and defaults to two files at a time.

**Translate a single file into English:**

```bash
python3 transub.py '/path/to/video.mkv' --target-language en
```

Select **English (Codex translation)** in your player. Other non-Korean track titles follow `<English language name> (Codex translation)`; Korean retains **한국어 (Codex 번역)**. New tracks use the corresponding [Matroska ISO 639-2 language metadata](https://www.matroska.org/technical/notes.html#language-codes).

**Process a folder and all its subfolders in Korean (the default):**

```bash
python3 transub.py '/path/to/videos' --work-dir './work/series'
```

Transub snapshots all regular `.mkv` files at startup, case-insensitively, sorts them by relative path, and processes up to two at a time. File and directory symlinks and `.transub-mux-*` temporary directories are excluded. A folder with no MKVs is an error. Folder input does not accept `-o`; each successful file replaces its original.

Each file receives its own argument copy and job directory. Files with subtitles in the selected target language are skipped, and an individual failure does not stop the remaining files. Options such as `--title` and `--source-track` apply to every file, so run titles or track layouts that differ separately.

**Process four files concurrently, or force sequential processing:**

```bash
python3 transub.py '/path/to/videos' --jobs 4
python3 transub.py '/path/to/videos' --jobs 1
```

`--jobs` limits concurrent files, not translation batches within a file; each file's batches remain sequential. A higher value can create that many simultaneous Codex requests and FFmpeg jobs, so choose it for your account limits, CPU, memory, and temporary disk space. It has no effect on a single-video input.

**Translate a folder recursively into Japanese:**

```bash
python3 transub.py '/path/to/videos' --target-language ja --work-dir './work/ja' --default-subtitle
```

**Provide a title hint and select Korean by default:**

```bash
python3 transub.py '/path/to/video.mkv' \
  --title 'TITLE_001' --default-subtitle
```

**Choose a reasoning level in a separate job directory:**

```bash
python3 transub.py '/path/to/video.mkv' --reasoning high --work-dir './work/high'
python3 transub.py '/path/to/video.mkv' --reasoning low --work-dir './work/low'
```

`low`, `medium`, `high`, and `xhigh` are accepted. Translation quality and speed have not been benchmarked across these settings.

**Set and attach a Korean font:**

```bash
python3 transub.py '/path/to/video.mkv' \
  --font 'Noto Sans CJK KR' --font-file '/path/to/font.otf'
```

Match `--font` to the font's internal family name. Inline ASS `\fn` overrides remain intact.

**Keep the original by writing a separate MKV, including from an MP4 input:**

```bash
python3 transub.py '/path/to/video.mp4' -o '/path/to/output.mkv'
```

A separate output must end in `.mkv` and must not already exist. Single-file input in other containers requires this output mode.

## Options

| Option | Behavior / default |
|---|---|
| `video` | Required video file or folder; folders recurse automatically. |
| `-o`, `--output` | Separate MKV output for a single file; omitted: replace the original MKV. |
| `--work-dir` | Job directory for one file, or parent of per-file job directories for a folder. |
| `--jobs N` | Maximum files processed concurrently in a folder; positive integer, default: `2`; `1` is sequential. No effect on a single file. |
| `--list-tracks` | List embedded subtitle tracks only; no translation or video changes. |
| `--source-track` | FFprobe subtitle stream index; automatic priority: Japanese → English → other, excluding the selected target language. |
| `--target-language CODE` | Target language; default: `ko` (Korean). Supported two- and three-letter codes appear below. |
| `--title` | Title, season, or episode hint; otherwise inferred from filename and subtitles. |
| `--model` | Research and translation model; default: `gpt-6.1-sol`. |
| `--reasoning` | `low`, `medium`, `high`, `xhigh`; new jobs: `medium`; resumed jobs: saved setting. |
| `--font` | Override ASS style font names; omitted: retain source fonts. |
| `--font-file` | Attach a TTF, OTF, or TTC font to the output MKV. |
| `--default-subtitle`, `--default-korean` | Make the new target-language track the default subtitle; omitted: retain existing default selection. |
| `--allow-existing-target`, `--allow-existing-korean` | Keep existing target-language tracks and add another; omitted: error for a single file, skip in folders. |

The legacy `--default-korean` and `--allow-existing-korean` aliases apply to the **selected target language**, including non-Korean targets.

<details>
<summary>Supported target-language codes (36 languages)</summary>

Use an ISO 639-1 code or its ISO 639-2 bibliographic/terminologic alias below. Codes are case-insensitive and normalize to the two-letter code (`ENG` → `en`, `fra` → `fr`). Unsupported codes and regional tags such as `en-US` or `pt-BR` are rejected. `--help` lists all supported two-letter codes.

| Language | ISO 639-1 | Accepted ISO 639-2 aliases |
|---|---|---|
| Korean | `ko` | `kor` |
| English | `en` | `eng` |
| Japanese | `ja` | `jpn` |
| Chinese | `zh` | `chi`, `zho` |
| Spanish | `es` | `spa` |
| French | `fr` | `fre`, `fra` |
| German | `de` | `ger`, `deu` |
| Portuguese | `pt` | `por` |
| Italian | `it` | `ita` |
| Russian | `ru` | `rus` |
| Ukrainian | `uk` | `ukr` |
| Arabic | `ar` | `ara` |
| Hindi | `hi` | `hin` |
| Bengali | `bn` | `ben` |
| Vietnamese | `vi` | `vie` |
| Thai | `th` | `tha` |
| Indonesian | `id` | `ind` |
| Malay | `ms` | `may`, `msa` |
| Turkish | `tr` | `tur` |
| Dutch | `nl` | `dut`, `nld` |
| Polish | `pl` | `pol` |
| Swedish | `sv` | `swe` |
| Norwegian | `no` | `nor` |
| Danish | `da` | `dan` |
| Finnish | `fi` | `fin` |
| Czech | `cs` | `cze`, `ces` |
| Greek | `el` | `gre`, `ell` |
| Hebrew | `he` | `heb` |
| Hungarian | `hu` | `hun` |
| Romanian | `ro` | `rum`, `ron` |
| Persian | `fa` | `per`, `fas` |
| Tamil | `ta` | `tam` |
| Telugu | `te` | `tel` |
| Urdu | `ur` | `urd` |
| Tagalog | `tl` | `tgl` |
| Swahili | `sw` | `swa` |

</details>

<details>
<summary>Advanced options and executable paths</summary>

| Option | Behavior / default |
|---|---|
| `--batch-size` | Events per translation batch; positive integer, default: `60`. |
| `--timeout` | Seconds per Codex request; positive integer, default: `900`. |
| `--retries` | Translation retries after failure; `0`–`5`, default: `2`. |
| `--auth-home` | Existing Codex home containing `auth.json`; default: `CODEX_HOME`, otherwise `~/.codex`. |
| `--codex` | Codex CLI executable; default: `codex`. |
| `--ffmpeg` | FFmpeg executable; default: `ffmpeg`. |
| `--ffprobe` | FFprobe executable; default: `ffprobe`. |
| `-h`, `--help` | Show command help. |

For a custom authentication directory, append `--auth-home '/path/to/codex-home'`.

</details>

## Resume and output

Rerun the same command from the **same working directory** to reuse saved research and validated translation batches. Keep `.transub` or your chosen `--work-dir`. Completed files in a folder are skipped by default once they contain subtitles in the selected target language. Repeat `--target-language` when resuming a non-Korean job: omitting it always means Korean and does not infer the language from saved settings.

The default job directory is `.transub/<stem-pathhash>/` for Korean and `.transub/<stem-pathhash>-<code>/` for other targets, under the directory where you run the command. For a single file, `--work-dir` is the exact job directory; no language suffix is added. For a folder, it is the parent of unique per-video directories, including for files with identical names in different subfolders; those child directories also receive `-<code>` for non-Korean targets.

Changing the target language, source, model, reasoning, title hint, source track, or batch size requires a new job directory. The manifest records the canonical two-letter target code. Legacy manifests with no target language are treated as Korean, and their valid Korean batches remain reusable. When `--reasoning` is omitted on resume, the saved setting is retained, including automatic Codex selection for legacy jobs that stored no explicit reasoning level. A changed translation protocol or context can invalidate individual cached batches.

| Artifact | Contents |
|---|---|
| `source.ass` | Extracted source subtitles. |
| `korean.ass` / `translated.<code>.ass` | Translated subtitles: `korean.ass` for Korean; for example, `translated.en.ass` for English. |
| `manifest.json` | Source, settings, and canonical target-language code used for resume checks. |
| `research.json` | Background, terminology, speech guidance, and sources. |
| `batch-*.json` | Validated translation checkpoints. |
| `codex-audit.json` | Request types, usage, timing, and isolation checks. |
| `verification.json` | Completion status, event/track counts, A/V hashes, and `target_language`, `translated_subtitle`, and `translated_stream_index`. |

Korean reports also retain the legacy `korean_subtitle` and `korean_stream_index` aliases. Research, batch, audit, and verification filenames remain the same for every target language.

Live progress from concurrent files can interleave; every progress line is prefixed with its relative path and snapshot position. Track listings and the final failure list follow the original relative-path order regardless of completion order. Terminal messages are currently in Korean. A folder summary looks like:

```text
처리 완료: 총 12개 · 완료 11개 · 건너뜀 1개 · 실패 0개
```

This means **12 total, 11 completed, 1 skipped, 0 failed**. Failures also list the relative path and reason. Exit codes are `0` for no failures (including skips), `1` for failures, and `130` for interruption with Ctrl+C.

Ctrl+C stops submitting new files, cancels and reaps running Codex and FFmpeg child processes, and waits for worker cleanup. A file that completed verification may already have atomically replaced its source; unfinished files keep their reusable research and validated batch checkpoints for the same command.

## Support and limitations

| Supported | Limitations |
|---|---|
| Embedded ASS, SSA, SRT, WebVTT, `mov_text`, and `text` subtitles | No PGS/VobSub, burned-in subtitles, OCR, or speech transcription. |
| MKV replacement; other single-file containers via `-o output.mkv` | Folder discovery includes MKV only. |
| Original ASS timing, styles, tags, layers, drawings, and overlaps | No automatic rearrangement of overlapping text; original karaoke timing may not match the translated syllables. |
| macOS and Linux; Python standard library | FFmpeg, FFprobe, and compatible Codex CLI are external requirements. |

Automatic source selection prefers **Japanese → English → other languages**, excludes the selected target language, and favors full dialogue over signs, songs, or forced tracks. Korean can be selected as a source when translating into another language. Language tags are preferred, with track-title hints as a fallback. For missing or incorrect metadata, inspect tracks and use `--source-track`.

Non-ASS text subtitles are converted to ASS with FFmpeg. Fonts need glyphs for the selected target language; use a player fallback font or `--font` / `--font-file` when necessary. Meaning, speaker interpretation, and politeness levels still need human review.

## Privacy and verification

**Filenames, subtitle text, and title hints are sent to Codex. Video files stay local.** Research uses Codex web search; translation requests have web search disabled. Local job artifacts retain subtitle text and research, so treat the work directory accordingly.

Transub copies only `auth.json`, with permissions `0600`, into a private temporary `CODEX_HOME`, and uses an empty working directory. It does not copy personal configuration, MCP settings, rules, skills, plugins, or session history. The temporary home is removed on exit; refreshed credentials are not written back to your original Codex home. System and organization policies still apply.

Before replacing an original, Transub verifies subtitle event fields, packet delivery order, existing streams and attachments, and video/audio stream SHA-256 hashes. It then atomically replaces the source. A failure before replacement leaves the original intact; there is no automatic backup. Avoid modifying the source with another application while processing.

<details>
<summary>Isolation and media verification details</summary>

- Requires `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, `--output-schema`, and `skip_host_skill_discovery`. Missing features cause failure rather than reduced isolation. The skill-discovery flag is experimental and needs rechecking after CLI upgrades.
- Uses an allowlisted process environment, a read-only sandbox, and disabled tool features. Checks that the isolated MCP list is empty and rejects recorded MCP or shell calls. Research must record actual web-search use.
- Does not save authentication values or raw CLI stderr in result artifacts. Consult the official [CLI reference](https://learn.chatgpt.com/docs/cli/reference) and [authentication guide](https://learn.chatgpt.com/docs/auth) for Codex behavior.
- Re-extracts the translated subtitles and compares every event field. Checks actual packet read order so subtitles are not delivered after their scenes, and compares attachment bytes and metadata.
- Uses `max_interleave_delta=0` for MKV output; long subtitle gaps can increase FFmpeg buffer memory. Original ASS event order and ReadOrder are retained.
- Allows only a narrowly verified HEVC `hvcC` `array_completeness` bit change from `1` to `0` during Matroska remuxing: structure and size must match, with no other byte changes. Other stream changes remain errors; A/V hashes and subtitle checks must still pass.
- Locks the source and job directory against concurrent Transub jobs and checks for source changes again before replacement.

</details>

## Troubleshooting

| Symptom | What to do |
|---|---|
| Missing `auth.json` or keychain-only login | Use the file-storage login command in [Quick start](#quick-start), or pass `--auth-home` for the correct Codex home. |
| Authentication expires during a long job | Sign in again in your original Codex home, then rerun the same command to reuse checkpoints. |
| Required isolation features are missing | Install a compatible Codex CLI; Transub stops if required safeguards are unavailable. |
| Model is unavailable for your account | Choose an available model with `--model` and a new `--work-dir`; no automatic fallback occurs. |
| Subtitles in the target language already exist | This is an error for single-file input and a skip for folders; use `--allow-existing-target` only to add another track. |
| Target-language code is rejected | Choose a supported code from `--help` or the table above; use a base language such as `en`, not a regional tag such as `en-US`. |
| No supported text subtitle track | Use an input with embedded supported text subtitles; image subtitles and audio are not transcribed. |
| Source or settings differ from the saved job | Restore the original command or choose a new `--work-dir`. |
| Translated text is missing or shows boxes | Reopen the video, select the new translation track, and check font support for the target language. |
| Stream, hash, or packet verification fails | Keep the work directory and inspect the reported mismatch. Correct the cause before retrying the same command; verification failure does not replace the original. |
| Not enough disk space | Free temporary space for the combined size of up to `--jobs` concurrently processed source files, or retry with fewer jobs. |

## Development

Run the regression suite from the project directory:

```bash
python3 -m unittest discover -s tests -v
```

Tests use mocked Codex responses and small synthetic videos generated with real FFmpeg. They cover replacement, A/V preservation, subtitle overlaps, failure handling, and resume behavior. They do not measure translation quality or require a live Codex translation session.
