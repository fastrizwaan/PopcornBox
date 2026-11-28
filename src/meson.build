pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'popcornbox'
gnome = import('gnome')

blueprints = custom_target('blueprints',
  input: files(
    'history.blp',
    'playlist.blp',
    'preferences.blp',
    'options.blp',
    'shortcuts-dialog.blp',
    'window.blp',
  ),
  output: '.',
  command: [find_program('blueprint-compiler'), 'batch-compile', '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
)

gnome.compile_resources('popcornbox',
  'popcornbox.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
)

python = import('python')

conf = configuration_data()
conf.set('PYTHON', python.find_installation('python3').full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'popcornbox.in',
  output: 'popcornbox',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)

popcornbox_sources = [
  '__init__.py',
  'api.py',
  'backup.py',
  'company_dialog.py',
  'database.py',
  'doh.py',
  'download_row.py',
  'history.py',
  'hls_proxy.py',
  'seek_proxy.py',
  'skip_segments.py',
  'http_downloader.py',
  'libtorrent_stream.py',
  'local_server.py',
  'main.py',
  'movie_widget.py',
  'mpris.py',
  'options.py',
  'person_dialog.py',
  'person_page.py',
  'player.py',
  'playlist.py',
  'plugins.py',
  'custom_plugins.py',
  'custom_plugins_runner.py',
  'preferences.py',
  'save_session.py',
  'shortcuts.py',
  'stremio_sync.py',
  'subtitle_search_dialog.py',
  'tmdb_helper.py',
  'utils.py',
  'window.py',
  'nuvio_runner.bundle.js',
]

install_data(popcornbox_sources, install_dir: moduledir)
