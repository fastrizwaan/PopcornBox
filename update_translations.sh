#!/bin/bash
OUTPUT="po/io.github.fastrizwaan.PopcornBox.pot"
PACKAGE_NAME="io.github.fastrizwaan.PopcornBox"
ENCODING="UTF-8"
LANGUAGE_BLP="--language=JavaScript"
LINGUAS_FILE="po/LINGUAS"

grep -v '\.blp$' po/POTFILES.in > /tmp/POTFILES_POPCORNBOX
grep '\.blp$' po/POTFILES.in > /tmp/POTFILES_POPCORNBOX.blp

# create pot
xgettext --files-from=/tmp/POTFILES_POPCORNBOX \
         --output="$OUTPUT" --package-name="$PACKAGE_NAME" \
         --from-code="$ENCODING" --add-comments \
         --keyword=_ --keyword=C_:1c,2

# join pot
xgettext --files-from=/tmp/POTFILES_POPCORNBOX.blp \
         --output="$OUTPUT" --package-name="$PACKAGE_NAME" \
         --from-code="$ENCODING" --add-comments \
         --keyword=_ --keyword=C_:1c,2 \
         $LANGUAGE_BLP \
         --join-existing


rm /tmp/POTFILES_POPCORNBOX /tmp/POTFILES_POPCORNBOX.blp

sed -i 's/charset=CHARSET/charset=UTF-8/g' $OUTPUT

grep -v '^#' "$LINGUAS_FILE" | while read -r lang_file; do
    msgmerge --previous --backup=none --update "po/${lang_file}.po" "$OUTPUT" \
    || echo "Error with : $lang_file" >&2
done
