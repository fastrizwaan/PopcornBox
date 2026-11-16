#!/usr/bin/env python3
import os
import shutil
import subprocess
import tempfile

def update_gresource():
    repo_root = os.path.dirname(os.path.abspath(__file__))
    share_dir = os.path.join(repo_root, 'build-dir/files/share/popcorn-box')
    res_file_popcorn = os.path.join(share_dir, 'popcornbox.gresource')
    res_file_cine = os.path.join(share_dir, 'cine.gresource')
    source_res = res_file_popcorn if os.path.exists(res_file_popcorn) else res_file_cine

    if not os.path.exists(source_res):
        print("Existing gresource not found at", source_res)
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        lines = subprocess.check_output(['gresource', 'list', source_res]).decode().strip().splitlines()
        for r in lines:
            out_path = os.path.join(tmpdir, r.lstrip('/'))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            content = subprocess.check_output(['gresource', 'extract', source_res, r])
            with open(out_path, 'wb') as f:
                f.write(content)

        # Compile and update blueprints
        blp_files = ['window', 'preferences', 'options', 'playlist', 'history', 'shortcuts-dialog']
        for name in blp_files:
            blp_path = os.path.join(repo_root, f'src/{name}.blp')
            ui_path = os.path.join(repo_root, f'src/{name}.ui')
            if os.path.exists(blp_path):
                target_ui = os.path.join(tmpdir, f'io/github/fastrizwaan/PopcornBox/{name}.ui')
                os.makedirs(os.path.dirname(target_ui), exist_ok=True)
                try:
                    subprocess.check_call([
                        'flatpak', 'run', f'--filesystem={repo_root}', '--filesystem=/tmp',
                        '--command=blueprint-compiler', 'org.gnome.Sdk//50',
                        'compile', blp_path, '--output', target_ui
                    ])
                    if name in ('window', 'preferences'):
                        shutil.copyfile(target_ui, ui_path)
                except Exception as e:
                    print(f"Warning: could not compile {blp_path}: {e}")
                    if os.path.exists(ui_path):
                        shutil.copyfile(ui_path, target_ui)

        # Overwrite with updated style.css
        css_path = os.path.join(repo_root, 'src/style.css')
        if os.path.exists(css_path):
            with open(css_path, 'rb') as f:
                css_data = f.read()
            with open(os.path.join(tmpdir, 'io/github/fastrizwaan/PopcornBox/style.css'), 'wb') as f:
                f.write(css_data)

        # Overwrite with updated action icons
        actions_dir = os.path.join(repo_root, 'data/icons/scalable/actions')
        target_actions_dir = os.path.join(tmpdir, 'io/github/fastrizwaan/PopcornBox/icons/scalable/actions')
        os.makedirs(target_actions_dir, exist_ok=True)
        if os.path.exists(actions_dir):
            for icon_name in os.listdir(actions_dir):
                if icon_name.endswith('.svg'):
                    src_icon = os.path.join(actions_dir, icon_name)
                    dst_icon = os.path.join(target_actions_dir, icon_name)
                    shutil.copyfile(src_icon, dst_icon)
                    # Also keep cine- alias if popcornbox-
                    if icon_name.startswith('popcornbox-'):
                        cine_name = 'cine-' + icon_name[len('popcornbox-'):]
                        shutil.copyfile(src_icon, os.path.join(target_actions_dir, cine_name))

        # Overwrite with updated app icons
        svg_icon_path = os.path.join(repo_root, 'data/icons/scalable/apps/io.github.fastrizwaan.PopcornBox.svg')
        if os.path.exists(svg_icon_path):
            with open(svg_icon_path, 'rb') as f:
                svg_data = f.read()
            out_icon = os.path.join(tmpdir, 'io/github/fastrizwaan/PopcornBox/icons/scalable/apps/io.github.fastrizwaan.PopcornBox.svg')
            os.makedirs(os.path.dirname(out_icon), exist_ok=True)
            with open(out_icon, 'wb') as f:
                f.write(svg_data)

        png_128_path = os.path.join(repo_root, 'data/icons/128x128/apps/io.github.fastrizwaan.PopcornBox.png')
        if os.path.exists(png_128_path):
            with open(png_128_path, 'rb') as f:
                png_data = f.read()
            out_png = os.path.join(tmpdir, 'io/github/fastrizwaan/PopcornBox/icons/128x128/apps/io.github.fastrizwaan.PopcornBox.png')
            os.makedirs(os.path.dirname(out_png), exist_ok=True)
            with open(out_png, 'wb') as f:
                f.write(png_data)

        # Generate xml
        xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<gresources>', '  <gresource prefix="/">']
        for root, dirs, files in os.walk(tmpdir):
            for file in files:
                full = os.path.join(root, file)
                rel = os.path.relpath(full, tmpdir)
                if rel != 'resources.xml':
                    xml_lines.append(f'    <file alias="{rel}">{rel}</file>')
        xml_lines.append('  </gresource>')
        xml_lines.append('</gresources>')

        xml_path = os.path.join(tmpdir, 'resources.xml')
        with open(xml_path, 'w') as f:
            f.write('\n'.join(xml_lines))

        out_target = os.path.join(tmpdir, 'compiled.gresource')
        subprocess.check_call(['glib-compile-resources', f'--sourcedir={tmpdir}', xml_path, f'--target={out_target}'])

        shutil.copyfile(out_target, res_file_popcorn)
        shutil.copyfile(out_target, res_file_cine)
        print('Successfully updated popcornbox.gresource & cine.gresource! Size:', os.path.getsize(res_file_popcorn))

        # Ensure symlink for popcornbox python module exists in build-dir
        mod_popcorn = os.path.join(share_dir, 'popcornbox')
        mod_cine = os.path.join(share_dir, 'cine')
        if os.path.exists(mod_cine) and not os.path.exists(mod_popcorn):
            os.symlink('cine', mod_popcorn)

if __name__ == '__main__':
    update_gresource()
