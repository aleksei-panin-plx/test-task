#!/bin/sh
# Container start: check the read-only customer data (the unpacked cardforge-data-*.tar.gz),
# copy the mask-review evidence into the runs volume, restore model descriptors, run the command.
set -eu
HOME_DIR="${CARDFORGE_HOME:-/srv/cardforge}"
DATA="${CARDFORGE_DATA_DIR:-/data}"

# dataset/ and the frame images are bind-mounted by docker-compose.yml at their own paths: the
# core refuses symlinks that leave its root ("path escapes artifact root").
for f in dataset knowledge/frames/frame_preview.png knowledge/frames/occlusion_v2.png; do
  [ -e "$HOME_DIR/$f" ] || echo "warning: $HOME_DIR/$f missing (unpack the data asset into ./data)" >&2
done
# Evidence of the technical mask review (sha256-checked against dataset/ by the core).
if [ -d "$DATA/runs/stage02" ] && [ ! -e "$HOME_DIR/runs/stage02" ]; then
  mkdir -p "$HOME_DIR/runs"
  cp -R "$DATA/runs/stage02" "$HOME_DIR/runs/stage02"
fi
# The models volume hides the image's models/*/source.json: restore the descriptors.
for d in /srv/cardforge-image/models/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  mkdir -p "$HOME_DIR/models/$name"
  [ -f "$HOME_DIR/models/$name/source.json" ] || cp "$d/source.json" "$HOME_DIR/models/$name/source.json"
done
exec "$@"
