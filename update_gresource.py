#!/usr/bin/env python3
import os
import shutil
import subprocess
import tempfile

def update_gresource():
    repo_root = os.path.dirname(os.path.abspath(__file__))
    res_file = os.path.join(repo_root, 'build-dir/files/share/popcorn-box/cine.gresource')
    if not os.path.exists(res_file):
        print("cine.gresource not found at", res_file)
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        lines = subprocess.check_output(['gresource', 'list', res_file]).decode().strip().splitlines()
        for r in lines:
            out_path = os.path.join(tmpdir, r.lstrip('/'))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            content = subprocess.check_output(['gresource', 'extract', res_file, r])
            with open(out_path, 'wb') as f:
                f.write(content)

        # Overwrite with updated window.ui
        win_ui_path = os.path.join(repo_root, 'src/window.ui')
        with open(win_ui_path, 'rb') as f:
            win_ui = f.read()
        with open(os.path.join(tmpdir, 'io/github/fastrizwaan/PopcornBox/window.ui'), 'wb') as f:
            f.write(win_ui)

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

        shutil.copyfile(out_target, res_file)
        print('Successfully updated cine.gresource! Size:', os.path.getsize(res_file))

if __name__ == '__main__':
    update_gresource()
