# Popcorn Box  2.0

Watch movies, series, and stream videos seamlessly

This is version 2.0 beta WIP, to make nvida+wayland+gnome video player works; now using Cine player instead of basic mpv.


![Popcorn Box Screenshot](https://github.com/fastrizwaan/WineCharm/releases/download/1.3/1.png)

A Stremio-compatible native media client built specifically for GNU/Linux using **Python 3**, **GTK4**, and **Libadwaita**.

use STABLE 1.2 (nvida+wayland+gnome blanks video player)
### 🛠 Installation & Usage
Open this flatpak with Gnome Software: [io.github.fastrizwaan.PopcornBox.flatpak](https://github.com/fastrizwaan/popcorn-box/releases/download/1.2/io.github.fastrizwaan.PopcornBox.flatpak)

### Method 1: Install Flatpak via CLI (Recommended)

```bash
flatpak --user remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak --user install flathub org.gnome.Platform//50
wget -c https://github.com/fastrizwaan/popcorn-box/releases/download/1.2/io.github.fastrizwaan.PopcornBox.flatpak
flatpak install --user io.github.fastrizwaan.PopcornBox.flatpak
```

Run the application:
```bash
flatpak run io.github.fastrizwaan.PopcornBox
```

### Method 2: Build and Install via Flatpak
##### Current github has version 2.0-beta is being worked on using Cine player as backend.
The installation script compiles, sandboxes, and installs the application locally:

```bash
git clone https://github.com/fastrizwaan/PopcornBox.git
cd PopcornBox
chmod +x build_bundle.sh
./build_bundle.sh
```

#### Create flatpak-bundle (.flatpak file)
```
flatpak build-bundle ~/.local/share/flatpak/repo io.github.fastrizwaan.PopcornBox.flatpak io.github.fastrizwaan.PopcornBox
```
Licensed under the **GPL-3.0-or-later** license. See the `COPYING` file for details.
