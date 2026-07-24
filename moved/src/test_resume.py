import libtorrent as lt
import time
import os

info_hash = "f3b39744c688b7d90ee4fc7a6e147df9e75ebcc4" # arbitrary
resume_path = "test.fastresume"

session = lt.session()
atp = lt.add_torrent_params()
atp.info_hash = lt.sha1_hash(bytes.fromhex(info_hash))
atp.save_path = "."

handle = session.add_torrent(atp)
print("Initial:", handle.status().all_time_upload, handle.status().all_time_download)
