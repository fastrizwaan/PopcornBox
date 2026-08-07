#!/usr/bin/env bash

# Builds the local PopcornBox v2.1 using its Flatpak manifest
cd "$(dirname "$0")"

echo "Checking for shared-modules..."
if [ -z "$(ls -A build-aux/flatpak/shared-modules)" ]; then
    echo "Fetching shared-modules..."
    git clone https://github.com/flathub/shared-modules.git build-aux/flatpak/shared-modules
fi

echo "Building PopcornBox v2.1 flatpak..."
flatpak-builder --user --install --force-clean --disable-rofiles-fuse build-dir build-aux/flatpak/io.github.fastrizwaan.PopcornBox.json
echo "Build complete! You can run it with: flatpak run io.github.fastrizwaan.PopcornBox"
