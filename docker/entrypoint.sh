#!/bin/sh
set -e

mkdir -p "$BEETSDIR" "$BEETSUI_DATA"

# First start: give beets a working config.
if [ ! -f "$BEETSDIR/config.yaml" ]; then
    echo "beets-ui: no $BEETSDIR/config.yaml found, writing a default one"
    cp /usr/local/share/beets-ui/config.default.yaml "$BEETSDIR/config.yaml"
fi

# Optional extra Python packages for beets plugins (persisted in /config).
if [ -n "$EXTRA_PIP_PACKAGES" ]; then
    target=/config/beets-ui/pip
    marker="$target/.installed"
    if [ "$(cat "$marker" 2>/dev/null)" != "$EXTRA_PIP_PACKAGES" ]; then
        echo "beets-ui: installing extra packages: $EXTRA_PIP_PACKAGES"
        rm -rf "$target"
        mkdir -p "$target"
        # shellcheck disable=SC2086
        pip install --quiet --target "$target" -c /venv/constraints.txt $EXTRA_PIP_PACKAGES
        echo "$EXTRA_PIP_PACKAGES" > "$marker"
    fi
fi

exec "$@"
