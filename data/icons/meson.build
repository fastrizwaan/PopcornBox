application_id = 'io.github.fastrizwaan.PopcornBox'

install_data(
  'scalable' / 'apps' / ('@0@.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps'
)

install_data(
  'symbolic' / 'apps' / ('@0@-symbolic.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'symbolic' / 'apps'
)

icon_sizes = ['16x16', '24x24', '32x32', '48x48', '64x64', '128x128', '256x256', '512x512']
foreach size : icon_sizes
  install_data(
    size / 'apps' / ('@0@.png').format(application_id),
    install_dir: get_option('datadir') / 'icons' / 'hicolor' / size / 'apps'
  )
endforeach

