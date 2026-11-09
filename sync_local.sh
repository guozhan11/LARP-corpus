#!/bin/bash
# Keep a local working copy of the corpus data in step with the T7 disk.
#
#   ./sync_local.sh pull   T7 -> ~/LARP_corpus_data and ~/Desktop/LARP_assembled
#                          (run after rerunning OCR or assembly on T7)
#   ./sync_local.sh push   ~/LARP_corpus_data/analysis -> T7 (backup of analysis output)
#
# Add -n to see what would change without copying, e.g. ./sync_local.sh pull -n
# pull mirrors the pipeline outputs: files no longer on T7 are removed from the
# local copies. analysis/ is never deleted from either side.
set -euo pipefail

T7=/Volumes/T7/LARP_corpus_data
LOCAL=$HOME/LARP_corpus_data
DESKTOP=$HOME/Desktop/LARP_assembled
OCR=batch1

[ -d "$T7" ] || { echo "T7 is not mounted ($T7)."; exit 1; }
DRY=()
[ "${2:-}" = "-n" ] && DRY=(--dry-run --itemize-changes)

case "${1:-}" in
  pull)
    mkdir -p "$LOCAL/ocr" "$DESKTOP"
    rsync -a --delete ${DRY[@]+"${DRY[@]}"} "$T7/assembled/" "$LOCAL/assembled/"
    rsync -a --delete ${DRY[@]+"${DRY[@]}"} "$T7/assembled/" "$DESKTOP/"
    rsync -a --delete ${DRY[@]+"${DRY[@]}"} "$T7/snapshot/" "$LOCAL/snapshot/"
    rsync -a --delete ${DRY[@]+"${DRY[@]}"} "$T7/ocr/$OCR" "$T7/ocr/${OCR}_fill" "$LOCAL/ocr/"
    rsync -a ${DRY[@]+"${DRY[@]}"} "$T7/books.csv" "$LOCAL/"
    rsync -au ${DRY[@]+"${DRY[@]}"} "$T7/analysis/" "$LOCAL/analysis/"   # -u, no --delete: keeps newer local work
    ;;
  push)
    rsync -au ${DRY[@]+"${DRY[@]}"} "$LOCAL/analysis/" "$T7/analysis/"
    ;;
  *)
    sed -n '2,10p' "$0"; exit 1 ;;
esac
echo "Done: $1"
