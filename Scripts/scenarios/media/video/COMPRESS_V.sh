#!/bin/bash
# COMP_V.sh - Batch AV1 video compressor (with COURSE mode)
# ============================================================
#  SETTINGS - edit here
# ============================================================

# --- Source directories (processed in order, top = highest priority) ---
# Use forward slashes! Git Bash mounts M: as /m/, P: as /p/, etc.
SOURCE_DIRS=()  # Configure explicitly before running this disabled legacy tool.

# ============================================================
#  COURSE DIRECTORIES
# ============================================================
# Files in COURSE_DIRS get special educational-content settings:
#   24fps cap, max 1920x1080, stronger AV1 compression.
#   Integrity check always runs (files may be mid-download).
#
# To add more course dirs, append paths here:
COURSE_DIRS=()

# ============================================================
#  MAIN COMPRESSION SETTINGS (SOURCE_DIRS)
# ============================================================

# --- Suffixes to skip (files already processed/compressed) ---
# Case-insensitive. Add any new suffixes here.
SKIP_SUFFIXES=(
    "_av1"
    "_compressed"
    "_compr"
)

# --- Output format ---
OUTPUT_SUFFIX="_av1"        # suffix added before extension
OUTPUT_EXT="mkv"            # output container: mkv | mp4
                            # NOTE: mkv is strongly recommended - mp4 has
                            # very limited subtitle format support (only mov_text).
                            # mkv preserves ASS/SSA/SRT/PGS/HDMV-PGS as-is.

# --- Video scale (main dirs) ---
# Set to "" to disable scaling (keep original resolution)
MAX_WIDTH=""                # max output width; height auto-scaled

# --- FPS (main dirs) ---
# Set to "" to keep original fps
TARGET_FPS=""               # e.g. 24, 30, 60 | "" = original

# --- Pixel format ---
PIX_FMT="yuv420p10le"       # yuv420p | yuv420p10le

# --- AV1 encoder (main dirs) ---
AV1_CRF=28                  # quality: lower = better (18-55)
AV1_PRESET=6                # speed: 0 (slow/best) - 13 (fast/worst)
AV1_THREADS=0               # 0 = auto
AV1_PARAMS="tune=0:keyint=10s:enable-overlays=1:scd=1"

# --- Audio ---
AUDIO_CODEC="copy"          # libopus | aac | copy
AUDIO_BITRATE="96k"         # ignored when AUDIO_CODEC=copy

# --- Stall detection ---
STALL_TIMEOUT=10800         # seconds before killing a stalled job (3h)
STALL_CHECK_INTERVAL=20     # seconds between stall checks

# ============================================================
#  COURSE-SPECIFIC SETTINGS
# ============================================================
# These override the main settings ONLY for files in COURSE_DIRS.
#
# To tune:
#   COURSE_AV1_CRF    -- lower = better quality, larger file (range 18-55)
#   COURSE_AV1_PRESET -- lower = slower encode, better compression (range 0-13)
#   COURSE_MAX_WIDTH/HEIGHT -- max resolution box (AR always preserved)
#   COURSE_TARGET_FPS -- output fps hard cap
#   COURSE_AV1_PARAMS -- SVT-AV1 params (keyint=5s: better chapter seeking
#                        in long lectures; film-grain=0: no grain for screencasts)
#   COURSE_OUTPUT_SUFFIX -- change to "_course_av1" to visually distinguish
#                           course files from main-dir files in Explorer
COURSE_MAX_WIDTH=1920
COURSE_MAX_HEIGHT=1080
COURSE_TARGET_FPS=24
COURSE_AV1_CRF=30               # slightly higher than main (28) -- courses have
                                 # low motion: screen + talking head, compresses great
COURSE_AV1_PRESET=5             # one step slower than main (6), worth it for courses
COURSE_PIX_FMT="yuv420p10le"    # "yuv420p" if you need max player compatibility
COURSE_AUDIO_CODEC="copy"       # keep original audio tracks (lecture audio, etc.)
COURSE_OUTPUT_SUFFIX="_av1"     # set to "_course_av1" to distinguish from main
COURSE_AV1_PARAMS="tune=0:keyint=5s:enable-overlays=1:scd=1:film-grain=0:film-grain-denoise=0"
                                 # keyint=5s: shorter GOP = snappier seeking (lectures are
                                 #            long, people jump around a lot)
                                 # film-grain=0: no grain synthesis for screen content
                                 # film-grain-denoise=0: no grain denoising pass

# ============================================================
#  INTEGRITY CHECK SETTINGS
# ============================================================
# Verifies files are complete and not in active use before encoding.
# Catches: torrents mid-download, files being written by other apps,
#          corrupted/truncated files, files locked by another process.
#
# INTEGRITY_CHECK:
#   0 = disabled for SOURCE_DIRS (faster, no pre-checks)
#   1 = enabled for SOURCE_DIRS
# NOTE: COURSE_DIRS ALWAYS run integrity check, this flag only affects SOURCE_DIRS.
#
# INTEGRITY_WAIT:
#   Seconds to pause between two file size measurements (torrent detection).
#   Increase to 15-30 if files are on slow NAS/SMB shares.
#
# INTEGRITY_END_SECS:
#   Seconds of end-of-file to attempt decoding (truncation check).
#   Increase to 15-20 for very large files on slow storage.
INTEGRITY_CHECK=0               # 0 = off for SOURCE_DIRS | 1 = on
INTEGRITY_WAIT=5                # seconds between size snapshots
INTEGRITY_END_SECS=8            # seconds to decode from end of file

# ============================================================
#  INTERNAL - do not edit below unless you know what you're doing
# ============================================================

# --- Use first dir as base for shared log/lock files ---
BASE_DIR="${SOURCE_DIRS[0]}"
LOG_FILE="$BASE_DIR/compress.log"
SKIP_FILE="$BASE_DIR/compress_skipped.log"
ERR_FILE="$BASE_DIR/compress_errors.log"
LOCK_FILE="$BASE_DIR/compress.lock"

FFMPEG_PID=""
FFMPEG_PGID=""        # process group ID - used to kill all ffmpeg children
CURRENT_TMP=""
CURRENT_PROGRESS=""
CURRENT_ERR=""

# --- Associative array to track files already seen in this run ---
declare -A SEEN_FILES

cleanup() {
    echo "[ABORT] Cleaning up..."
    if [ -n "$FFMPEG_PID" ] && kill -0 "$FFMPEG_PID" 2>/dev/null; then
        kill -TERM "$FFMPEG_PID" 2>/dev/null
        sleep 2
        kill -KILL "$FFMPEG_PID" 2>/dev/null
    fi
    if [ -n "$FFMPEG_PGID" ] && [ "$FFMPEG_PGID" -gt 1 ] 2>/dev/null; then
        kill -KILL -"$FFMPEG_PGID" 2>/dev/null || true
    fi
    [ -n "$CURRENT_TMP" ]      && rm -f "$CURRENT_TMP"
    [ -n "$CURRENT_PROGRESS" ] && rm -f "$CURRENT_PROGRESS"
    [ -n "$CURRENT_ERR" ]      && rm -f "$CURRENT_ERR"
    rm -f "$LOCK_FILE"
    echo "[ABORT] Done."
    exit 0
}

trap cleanup SIGINT SIGTERM SIGHUP EXIT

# --- Lock check ---
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE")
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[ERROR] Already running (PID $LOCK_PID)"
        trap - EXIT
        exit 1
    else
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"

touch "$LOG_FILE" "$SKIP_FILE" "$ERR_FILE"

# --- Warn if mp4 chosen ---
if [ "$OUTPUT_EXT" = "mp4" ]; then
    echo "[WARN] OUTPUT_EXT=mp4: subtitle tracks may be dropped or converted."
    echo "[WARN] Only mov_text (SRT-derived) subtitles are supported in mp4."
    echo "[WARN] PGS/HDMV-PGS/ASS/SSA subtitles will be lost. Use mkv instead."
fi

# --- Cleanup orphaned tmp/progress/err files from ALL dirs ---
echo "[INIT] Cleaning up orphaned tmp/progress files..."
ALL_CLEANUP_DIRS=("${SOURCE_DIRS[@]}" "${COURSE_DIRS[@]}")
for dir in "${ALL_CLEANUP_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        find "$dir" -type f \( \
            -name "*.tmp.mkv"          -o \
            -name "*.tmp.mkv.progress" -o \
            -name "*.tmp.mkv.err"      -o \
            -name "*.tmp.mp4"          -o \
            -name "*.tmp.mp4.progress" -o \
            -name "*.tmp.mp4.err"      \
        \) -delete
    fi
done
echo "[INIT] Done."

# --- Print processing info ---
echo "[INFO] SOURCE_DIRS (normal mode):"
for i in "${!SOURCE_DIRS[@]}"; do
    echo "  [$((i+1))] ${SOURCE_DIRS[$i]}"
done
echo "[INFO] COURSE_DIRS (course mode: ${COURSE_MAX_WIDTH}x${COURSE_MAX_HEIGHT} / ${COURSE_TARGET_FPS}fps / CRF${COURSE_AV1_CRF}):"
for i in "${!COURSE_DIRS[@]}"; do
    echo "  [$((i+1))] ${COURSE_DIRS[$i]}"
done
echo "[INFO] Logs: done=$LOG_FILE  skip=$SKIP_FILE  err=$ERR_FILE"

# ============================================================
#  HELPER FUNCTIONS
# ============================================================

# --- Build vf filter chain for normal (SOURCE_DIRS) mode ---
build_vf() {
    local filters=()
    if [ -n "$MAX_WIDTH" ]; then
        filters+=("scale='min(${MAX_WIDTH},iw)':-2")
    fi
    if [ -n "$TARGET_FPS" ]; then
        filters+=("fps=${TARGET_FPS}")
    fi
    if [ "${#filters[@]}" -gt 0 ]; then
        local IFS=","
        echo "${filters[*]}"
    else
        echo ""
    fi
}

# --- Build vf filter chain for COURSE mode ---
# Applies: max 1920x1080 with AR preservation (no upscale), 24fps hard cap.
#
# Scale logic:
#   w=min(COURSE_MAX_WIDTH,iw)  and  h=min(COURSE_MAX_HEIGHT,ih)  set the
#   target box to never exceed source dims (prevents upscaling small videos).
#   force_original_aspect_ratio=decrease then fits the video into that box
#   while preserving AR — whichever dimension is the tighter constraint wins.
#   flags=lanczos: high-quality resampling (better sharpness on text/slides
#   than default bicubic — matters a lot for code screenshots and slides).
#
# To adjust: change COURSE_MAX_WIDTH, COURSE_MAX_HEIGHT, COURSE_TARGET_FPS above.
build_vf_course() {
    local W="$COURSE_MAX_WIDTH"
    local H="$COURSE_MAX_HEIGHT"
    local FPS="$COURSE_TARGET_FPS"
    local filters=()

    # Step 1: Downscale to max WxH, preserve AR, never upscale.
    # \, escapes commas inside FFmpeg expressions from the filtergraph parser.
    filters+=("scale=w=min(${W}\\,iw):h=min(${H}\\,ih):force_original_aspect_ratio=decrease:flags=lanczos")

    # Step 2: Ensure even dimensions (SVT-AV1 hard requirement).
    filters+=("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    # Step 3: Hard fps cap. 60fps lecture -> 24fps. Already-24 or 23.976 -> unchanged.
    filters+=("fps=fps=${FPS}")

    local IFS=","
    printf '%s' "${filters[*]}"
}

# --- Build find exclude args ---
build_find_excludes() {
    EXCLUDE_ARGS=()
    for suffix in "${SKIP_SUFFIXES[@]}"; do
        EXCLUDE_ARGS+=( ! -iname "*${suffix}.*" )
        EXCLUDE_ARGS+=( ! -iname "*${suffix}_*" )
    done
}

# --- Convert Git Bash Unix path to Windows path for native Win32 EXEs ---
to_winpath() {
    cygpath -w "$1" 2>/dev/null || echo "$1"
}

# --- Log ffmpeg error to ERR_FILE and print tail to console ---
log_ffmpeg_error() {
    local file="$1"
    local err_tmp="$2"
    local exit_code="$3"
    local stalled="$4"

    {
        echo "================================================================"
        echo "[FAIL] $(date '+%Y-%m-%d %H:%M:%S')  exit=$exit_code  stalled=$stalled"
        echo "[FILE] $file"
        echo "--- ffmpeg stderr ---"
        cat "$err_tmp" 2>/dev/null
        echo "--- end ---"
        echo ""
    } >> "$ERR_FILE"

    echo "[ERROR DETAILS] last lines of ffmpeg output:"
    grep -Ev "^\s*$|^Svt\[info\]|configuration:|lib(av|sw)" "$err_tmp" 2>/dev/null \
        | tail -10 \
        | sed 's/^/  /'
}

# ============================================================
#  INTEGRITY CHECK
# ============================================================
# Returns 0 if file is safe to encode, 1 if it should be skipped.
# Runs a 5-stage gauntlet:
#   1. Size stability   -- detects active torrent downloads / writes
#   2. Windows lock     -- detects files held exclusively by another process
#   3. Video stream     -- detects corrupted/unreadable headers
#   4. Duration         -- detects files ffprobe can open but not measure
#   5. End-of-file decode -- detects truncated/incomplete files
#
# To tune:
#   INTEGRITY_WAIT     -- seconds between size snapshots (stage 1)
#   INTEGRITY_END_SECS -- seconds of end-of-file to decode (stage 5)
check_file_integrity() {
    local file="$1"
    local wfile
    wfile=$(to_winpath "$file")
    local base
    base=$(basename "$file")

    echo "[CHECK] Integrity: $base"

    # --- Stage 1: Size stability (torrent/active write detection) ---
    local size1 size2
    size1=$(stat -c%s "$file" 2>/dev/null || echo "0")
    echo "[CHECK] Size=${size1}B — waiting ${INTEGRITY_WAIT}s for stability check..."
    sleep "$INTEGRITY_WAIT"
    size2=$(stat -c%s "$file" 2>/dev/null || echo "0")
    if [ "$size1" != "$size2" ]; then
        echo "[INTEGRITY] SKIP: size ${size1} -> ${size2} (file is being written). Skip."
        return 1
    fi

    # --- Stage 2: Windows file lock check (exclusive open attempt) ---
    # Uses .NET FileShare=None — if another process has the file open with
    # exclusive write lock (torrent client seeding, download manager, etc.),
    # this will fail. Read-only locks (e.g., media player) won't block us.
    local lock_status
    lock_status=$(powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
\$p = '$( echo "$wfile" | sed "s/'/''/g" )'
try {
    \$s = [System.IO.File]::Open(\$p, 'Open', 'Read', 'None')
    \$s.Close()
    Write-Output 'ok'
} catch {
    Write-Output 'locked'
}
" 2>/dev/null | tr -d '\r\n')
    if [ "$lock_status" = "locked" ]; then
        echo "[INTEGRITY] SKIP: File is exclusively locked by another process."
        return 1
    fi

    # --- Stage 3: Video stream / header check ---
    if ! ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_type -of compact=p=0:nk=1 \
        "$wfile" 2>/dev/null | grep -q "video"; then
        echo "[INTEGRITY] SKIP: No readable video stream (corrupt header?)."
        return 1
    fi

    # --- Stage 4: Duration check ---
    local duration
    duration=$(ffprobe -v error \
        -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 \
        "$wfile" 2>/dev/null | tr -d '\r' | head -1)
    if [ -z "$duration" ] || [ "$duration" = "N/A" ]; then
        echo "[INTEGRITY] SKIP: Cannot determine file duration."
        return 1
    fi

    # --- Stage 5: End-of-file decode check (truncation detection) ---
    # Seeks near the end and tries to decode INTEGRITY_END_SECS seconds.
    # A truncated file (interrupted download, bad copy) fails here.
    local seek_time
    seek_time=$(awk "BEGIN { t=$duration - $INTEGRITY_END_SECS; print (t < 0) ? 0 : t }")
    if ! ffmpeg -nostdin -v error \
        -ss "$seek_time" -i "$wfile" \
        -t "$INTEGRITY_END_SECS" \
        -f null - </dev/null 2>/dev/null; then
        echo "[INTEGRITY] SKIP: End of file not decodable — file likely truncated."
        return 1
    fi

    echo "[CHECK] OK: File is complete and readable."
    return 0
}

# ============================================================
#  MAIN LOOP
# ============================================================

# Build combined dir list with mode tags (format: "mode|/path")
# SOURCE_DIRS processed first, then COURSE_DIRS.
ALL_DIRS_TAGGED=()
for d in "${SOURCE_DIRS[@]}"; do
    ALL_DIRS_TAGGED+=("normal|$d")
done
for d in "${COURSE_DIRS[@]}"; do
    ALL_DIRS_TAGGED+=("course|$d")
done

for DIR_ENTRY in "${ALL_DIRS_TAGGED[@]}"; do

    MODE="${DIR_ENTRY%%|*}"
    SOURCE_DIR="${DIR_ENTRY#*|}"

    if [ ! -d "$SOURCE_DIR" ]; then
        echo "[SKIP] Directory not found: $SOURCE_DIR"
        continue
    fi

    echo ""
    if [ "$MODE" = "course" ]; then
        echo "[DIR] [COURSE] $SOURCE_DIR"
        echo "[DIR] [COURSE] Settings: fps=${COURSE_TARGET_FPS} max=${COURSE_MAX_WIDTH}x${COURSE_MAX_HEIGHT} crf=${COURSE_AV1_CRF} preset=${COURSE_AV1_PRESET}"
    else
        echo "[DIR] Processing: $SOURCE_DIR"
    fi

    # --- Build exclude args ---
    build_find_excludes

    # --- Build file list (recursive, all common video formats) ---
    mapfile -d '' FILE_LIST < <(find "$SOURCE_DIR" -type f \
        \( \
            -iname "*.mp4"  -o \
            -iname "*.mkv"  -o \
            -iname "*.avi"  -o \
            -iname "*.mov"  -o \
            -iname "*.wmv"  -o \
            -iname "*.webm" -o \
            -iname "*.ts"   -o \
            -iname "*.m2ts" -o \
            -iname "*.m4v"  -o \
            -iname "*.flv"  -o \
            -iname "*.vob"  -o \
            -iname "*.mpg"  -o \
            -iname "*.mpeg" \
        \) \
        "${EXCLUDE_ARGS[@]}" \
        ! -name "*.tmp.*" \
        ! -name "._*" \
        -print0)

    if [ "${#FILE_LIST[@]}" -eq 0 ]; then
        echo "[SKIP] No eligible files in: $SOURCE_DIR"
        continue
    fi

    for file in "${FILE_LIST[@]}"; do

        # --- Deduplicate across overlapping dirs ---
        real_file=$(realpath "$file" 2>/dev/null || echo "$file")
        if [ -n "${SEEN_FILES[$real_file]+x}" ]; then
            echo "[SKIP] Already processed this run (overlapping dirs): $file"
            continue
        fi
        SEEN_FILES["$real_file"]=1

        # --- Skip if already in done/skip logs ---
        if grep -Fxq "$file" "$LOG_FILE"; then
            echo "[SKIP] Already in done log: $file"
            continue
        fi
        if grep -Fxq "$file" "$SKIP_FILE"; then
            echo "[SKIP] Already in skip log: $file"
            continue
        fi

        # --- Select mode-specific settings ---
        if [ "$MODE" = "course" ]; then
            active_crf="$COURSE_AV1_CRF"
            active_preset="$COURSE_AV1_PRESET"
            active_params="$COURSE_AV1_PARAMS"
            active_pix_fmt="$COURSE_PIX_FMT"
            active_audio_codec="$COURSE_AUDIO_CODEC"
            active_suffix="$COURSE_OUTPUT_SUFFIX"
            do_integrity=1                  # always check integrity for course dirs
        else
            active_crf="$AV1_CRF"
            active_preset="$AV1_PRESET"
            active_params="$AV1_PARAMS"
            active_pix_fmt="$PIX_FMT"
            active_audio_codec="$AUDIO_CODEC"
            active_suffix="$OUTPUT_SUFFIX"
            do_integrity="$INTEGRITY_CHECK"
        fi

        output="${file%.*}${active_suffix}.${OUTPUT_EXT}"
        tmp_output="${file%.*}${active_suffix}.tmp.${OUTPUT_EXT}"
        progress_file="${tmp_output}.progress"
        err_tmp="${tmp_output}.err"

        # --- Skip if output already exists ---
        if [ -f "$output" ]; then
            echo "[DONE] Output already exists, marking: $file"
            echo "$file" >> "$LOG_FILE"
            continue
        fi

        rm -f "$tmp_output" "$progress_file" "$err_tmp"

        _wfile=$(to_winpath "$file")

        # --- Integrity check (always for course, optional for normal) ---
        # If integrity check passes, video stream is already confirmed (stage 3).
        # If integrity check is disabled, we run a quick video stream check instead.
        video_confirmed=0
        if [ "$do_integrity" -eq 1 ]; then
            if ! check_file_integrity "$file"; then
                echo "$file" >> "$SKIP_FILE"
                continue
            fi
            video_confirmed=1
        fi

        # --- Quick video stream check (only if integrity check was skipped) ---
        if [ "$video_confirmed" -eq 0 ]; then
            if ! ffprobe -v error -select_streams v:0 \
                -show_entries stream=codec_type -of compact=p=0:nk=1 "$_wfile" 2>/dev/null \
                | grep -q "video"; then
                echo "[SKIP] No video stream (audio-only?): $file"
                echo "$file" >> "$SKIP_FILE"
                continue
            fi
        fi

        # --- Count tracks ---
        sub_count=$(ffprobe -v error -select_streams s \
            -show_entries stream=index -of compact=p=0:nk=1 "$_wfile" 2>/dev/null | tr -d '\r' | grep -c ".")
        att_count=$(ffprobe -v error -select_streams t \
            -show_entries stream=index -of compact=p=0:nk=1 "$_wfile" 2>/dev/null | tr -d '\r' | grep -c ".")
        audio_stream_count=$(ffprobe -v error -select_streams a \
            -show_entries stream=index -of compact=p=0:nk=1 "$_wfile" 2>/dev/null | tr -d '\r' | grep -c ".")

        if [ "$MODE" = "course" ]; then
            echo "[ENCODE][COURSE] $file"
        else
            echo "[ENCODE] $file"
        fi
        echo "[INFO]   subtitles=$sub_count  attachments(fonts)=$att_count  audio=$audio_stream_count"

        # --- Audio options ---
        if [ "$audio_stream_count" -eq 0 ]; then
            audio_opts="-an"
        else
            if [ "$active_audio_codec" = "copy" ]; then
                audio_opts="-c:a copy"
            else
                audio_opts="-c:a ${active_audio_codec} -b:a ${AUDIO_BITRATE}"
            fi
        fi

        # --- Video filter ---
        if [ "$MODE" = "course" ]; then
            VF=$(build_vf_course)
        else
            VF=$(build_vf)
        fi
        if [ -n "$VF" ]; then
            vf_opts=(-vf "$VF")
        else
            vf_opts=()
        fi

        CURRENT_TMP="$tmp_output"
        CURRENT_PROGRESS="$progress_file"
        CURRENT_ERR="$err_tmp"

        # -map 0:V        - real video streams ONLY (capital V = excludes attached pictures/cover art)
        # -map 0:a?       - audio streams (optional)
        # -map 0:s?       - subtitle streams (copy as-is)
        # -map 0:t?       - attachments: embedded fonts, cover art, etc.
        # NOTE: -map 0:d? intentionally excluded — data streams (tmcd, etc.)
        #   are NOT supported by Matroska muxer and cause a fatal error.

        ffmpeg -nostdin -i "$_wfile" \
            -map 0:V -map 0:a? -map 0:s? -map 0:t? \
            "${vf_opts[@]}" \
            -pix_fmt "$active_pix_fmt" \
            -c:v libsvtav1 -crf "$active_crf" -preset "$active_preset" -threads "$AV1_THREADS" \
            -svtav1-params "$active_params" \
            $audio_opts \
            -c:s copy \
            -c:t copy \
            -map_metadata 0 \
            -map_chapters 0 \
            -progress "$(to_winpath "$progress_file")" \
            -f matroska "$(to_winpath "$tmp_output")" \
            </dev/null 2>"$err_tmp" &

        FFMPEG_PID=$!
        FFMPEG_PGID=$(ps -o pgid= -p "$FFMPEG_PID" 2>/dev/null | tr -d ' \r\n')
        last_change=$(date +%s)
        stalled=0
        last_frame=0

        while kill -0 "$FFMPEG_PID" 2>/dev/null; do
            sleep "$STALL_CHECK_INTERVAL"
            if [ -f "$progress_file" ]; then
                current_frame=$(grep "^frame=" "$progress_file" | tail -1 | cut -d= -f2 | tr -d ' ')
                if [ -n "$current_frame" ] && [ "$current_frame" != "$last_frame" ]; then
                    last_frame=$current_frame
                    last_change=$(date +%s)
                fi
            fi

            elapsed=$(( $(date +%s) - last_change ))
            if [ "$elapsed" -gt "$STALL_TIMEOUT" ]; then
                echo "[STALL] Timeout reached, killing: $file"
                kill -KILL "$FFMPEG_PID" 2>/dev/null
                echo "$file" >> "$SKIP_FILE"
                stalled=1
                break
            fi
        done

        wait "$FFMPEG_PID"
        exit_code=$?
        FFMPEG_PID=""

        # --- Kill any lingering child processes ---
        if [ -n "$FFMPEG_PGID" ] && [ "$FFMPEG_PGID" -gt 1 ] 2>/dev/null; then
            kill -KILL -"$FFMPEG_PGID" 2>/dev/null || true
        fi
        FFMPEG_PGID=""

        if [ "$stalled" -eq 0 ] && [ "$exit_code" -eq 0 ]; then
            # Windows file handles can linger. Add retry loop to avoid silent failures!
            retry_count=0
            while [ $retry_count -lt 5 ]; do
                mv "$tmp_output" "$output" 2>/dev/null && break
                sleep 2
                retry_count=$((retry_count+1))
            done

            if [ -f "$output" ]; then
                retry_count=0
                while [ $retry_count -lt 5 ]; do
                    rm -f "$file" 2>/dev/null && break
                    sleep 2
                    retry_count=$((retry_count+1))
                done
            fi

            rm -f "$progress_file" "$err_tmp"
            echo "[DONE] $file"
            echo "$file" >> "$LOG_FILE"
        else
            log_ffmpeg_error "$file" "$err_tmp" "$exit_code" "$stalled"
            rm -f "$tmp_output" "$progress_file" "$err_tmp"
            [ "$stalled" -eq 0 ] && echo "$file" >> "$SKIP_FILE"
            echo "[FAIL] exit=$exit_code stalled=$stalled: $file"
        fi

        CURRENT_TMP=""
        CURRENT_PROGRESS=""
        CURRENT_ERR=""
    done

done

echo ""
echo "[DONE] All directories processed."
echo "[INFO] Error details: $ERR_FILE"
rm -f "$LOCK_FILE"