[1mdiff --git a/README.md b/README.md[m
[1mindex ea9d5a6..33d406d 100644[m
[1m--- a/README.md[m
[1m+++ b/README.md[m
[36m@@ -15,6 +15,7 @@[m [mA Linux media client for discovering and playing movies, series, anime, and othe[m
 - Use **Unique** to combine matching catalog results while removing duplicates. Discover opens with Unique enabled by default.[m
 - Search while filtering. Search checks titles across addon catalogs, handles punctuation and Unicode in search terms, supports title-only matching, and loads additional pages as you scroll.[m
 - Use the separate search view to search available catalogs and browse results by media type.[m
[32m+[m[32m- Direct stream results are checked against the complete title in order using consistent punctuation and symbol normalization, to avoid unrelated matches. Torrent retrieval keeps its separate existing matching behavior.[m
 [m
 ### Library and addons[m
 [m
[1mdiff --git a/readme.txt b/readme.txt[m
[1mindex 059c563..e7bb3f4 100644[m
[1m--- a/readme.txt[m
[1m+++ b/readme.txt[m
[36m@@ -80,6 +80,24 @@[m [mSearch and plugin retrieval:[m
 - Nuvio search passes the original trimmed query in the runner's JSON payload.[m
   Poll cancellation while a catalog runner is active and terminate it promptly[m
   when the search is superseded.[m
[32m+[m[32m- Exclude anime-focused Nuvio scrapers from non-anime movie and TV-series stream[m
[32m+[m[32m  requests, even when a legacy manifest declares broad `movie`/`tv` support. For[m
[32m+[m[32m  mixed searches, preserve torrent results from torrent-capable anime scrapers[m
[32m+[m[32m  but discard their direct results; remove matching cached direct cards too.[m
[32m+[m[32m- Keep custom-plugin route searches bound to the route's owning provider so an[m
[32m+[m[32m  unrelated provider cannot add streams to a 9kmovies or similar route. Clean[m
[32m+[m[32m  release slugs by extracting an embedded year before title matching. When a[m
[32m+[m[32m  custom catalog route supplies its own title and year, use those values for[m
[32m+[m[32m  stream matching instead of a potentially ambiguous metadata-provider title.[m
[32m+[m[32m- For direct streams from Nuvio, CloudStream, Stremio, and custom providers,[m
[32m+[m[32m  validate any informative title or filename against the complete requested[m
[32m+[m[32m  title-token sequence, in order and at the start of the title after recognized[m
[32m+[m[32m  provider, CDN, and release labels are removed. Normalize case, accents, punctuation,[m
[32m+[m[32m  and symbols consistently; preserve short and repeated title words. Reject[m
[32m+[m[32m  partial and out-of-order matches, and apply the same check to cached direct[m
[32m+[m[32m  streams. Generic provider labels and opaque URLs provide no title evidence, so[m
[32m+[m[32m  they remain subject to the existing checks. Keep torrent results on their[m
[32m+[m[32m  existing matching path; do not apply this stricter direct-stream check to them.[m
 [m
 Stack Homogeneity & Layout Integrity:[m
 - Both `main_stack` and `library_stack` must have `hhomogeneous: false` and[m
[36m@@ -386,14 +404,21 @@[m [mRuntime: `org.gnome.Platform//50` | SDK: `org.gnome.Sdk//50`[m
   and freezes the host operating system.[m
 - ALWAYS RUN INDIVIDUAL TESTS SEPARATELY:[m
   All unit tests live in `tests/`. Only execute the single test file relevant[m
[31m-  to the modified component, e.g.:[m
[31m-    python3 tests/test_genre_dropdown.py[m
[31m-    python3 tests/test_media_type_genre_separation.py[m
[31m-    python3 tests/test_catalog_filter_empty_recovery.py[m
[31m-    python3 tests/test_robust_filter_and_spinners.py[m
[32m+[m[32m  to the modified component, inside the Flatpak runtime. From the repository[m
[32m+[m[32m  root, expose the checkout and run one test file at a time, e.g.:[m
[32m+[m[32m    project_dir="$(pwd)"[m
[32m+[m[32m    flatpak run --filesystem="$project_dir" --command=sh io.github.fastrizwaan.PopcornBox \[m
[32m+[m[32m      -c 'cd "$1" && PYTHONPATH=. python3 "$2"' sh "$project_dir" tests/test_genre_dropdown.py[m
[32m+[m[32m  Substitute the relevant test path, for example[m
[32m+[m[32m  `tests/test_media_type_genre_separation.py`,[m
[32m+[m[32m  `tests/test_catalog_filter_empty_recovery.py`, or[m
[32m+[m[32m  `tests/test_robust_filter_and_spinners.py`.[m
 - RUN SPECIFIC METHODS WHEN DEBUGGING:[m
   For fast turnaround and minimal memory overhead, isolate specific test cases:[m
[31m-    python3 -m unittest tests/test_file.py -k TestClass.test_method[m
[32m+[m[32m    project_dir="$(pwd)"[m
[32m+[m[32m    flatpak run --filesystem="$project_dir" --command=sh io.github.fastrizwaan.PopcornBox \[m
[32m+[m[32m      -c 'cd "$1" && PYTHONPATH=. python3 -m unittest -k "$3" "$2"' \[m
[32m+[m[32m      sh "$project_dir" tests/test_file.py TestClass.test_method[m
 - SERIAL EXECUTION ONLY:[m
   Never run multiple unit test files in parallel background jobs or loops. Always[m
   wait for one test process to finish cleanly before launching another.[m
[1mdiff --git a/src/api.py b/src/api.py[m
[1mindex 4ae72b7..9a67545 100755[m
[1m--- a/src/api.py[m
[1m+++ b/src/api.py[m
[36m@@ -5341,10 +5341,30 @@[m [mdef clean_media_title(raw_title):[m
     if not text:[m
         return "", None[m
     year_match = re.search(r"[\s(\[-]*((?:19|20)\d{2})[\s)\]-]*$", text)[m
[32m+[m[32m    if not year_match:[m
[32m+[m[32m        # Scraper slugs often put the year before release labels rather than at[m
[32m+[m[32m        # the very end (e.g. "Title 2026 English Short Film 720p HDRip").[m
[32m+[m[32m        # Use the last embedded year so the release tail cannot become part of[m
[32m+[m[32m        # the title sent to metadata and stream matching.[m
[32m+[m[32m        release_tail_marker = re.compile([m
[32m+[m[32m            r"\b(?:short\s+film|web\s+series|english|hindi|dual\s+audio|"[m
[32m+[m[32m            r"dubbed|subbed|hdrip|web-?dl|webrip|bluray|blu-ray|2160p|1080p|"[m
[32m+[m[32m            r"720p|480p|4k|x264|x265|h\.?265|h\.?264|aac|hevc)\b",[m
[32m+[m[32m            re.IGNORECASE,[m
[32m+[m[32m        )[m
[32m+[m[32m        embedded_years = [[m
[32m+[m[32m            match for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)[m
[32m+[m[32m            if ([m
[32m+[m[32m                match.start(1) > 0[m
[32m+[m[32m                and release_tail_marker.search(text[match.end(1):])[m
[32m+[m[32m            )[m
[32m+[m[32m        ][m
[32m+[m[32m        if embedded_years:[m
[32m+[m[32m            year_match = embedded_years[-1][m
     extracted_year = year_match.group(1) if year_match else None[m
     clean_name = text[m
     if year_match:[m
[31m-        clean_name = text[:year_match.start()].strip(" .-_([")[m
[32m+[m[32m        clean_name = text[:year_match.start(1)].strip(" .-_([")[m
     clean_name = re.sub([m
         r"\s*(?:\b(?:4k|2160p|1080p|720p|480p|hdrip|web-?dl|webrip|bluray|dual audio|hindi dubbed)\b.*)$",[m
         "",[m
[36m@@ -6478,11 +6498,13 @@[m [m_PROVIDER_PREFIX_WORDS = {[m
     '1shows', 'vidrock', 'vidsrc', 'vidfast', 'vidlink', 'vidlove', 'movix', 'castle',[m
     'dahmermovies', 'cinestream', 'torrentio', 'yts', 'yify', 'eztv', 'nyaa', 'rarbg', '1337x',[m
     'topcartoons', 'uhdmovies', 'hdhub4u', '4khdhub', 'hdhub', 'movies4u', 'bollyflix',[m
[31m-    'moviesmod', 'vegamovies', 'moviesdrive', 'moviebox', 'purstream', 'streamflix',[m
[32m+[m[32m    'moviesmod', 'vegamovies', 'moviesdrive', '9kmovies', 'moviebox', 'purstream', 'streamflix',[m
     'fibwatch', 'desiflix', 'einthustan', 'hindmoviez', 'cinefreak', 'cinemacity',[m
     'moonflix', 'allmovieland', 'reanime', 'hianime', 'allanime', 'animekai', 'animepahe',[m
[31m-    'fast', 'server', 'premium', 'shared', 'hubcloud', 'hubdrive', 'hubcdn', 'driveseed',[m
[31m-    'gdflix', 'gdtot', 'filepress', 'vcloud', 'fsl', 'gofile', 'vikingfile', 'buzzheavier',[m
[32m+[m[32m    'fast', 'server', 'premium', 'shared', 'hubcloud', 'hubdrive', 'hubcdn', 'cloud', 'google', 'cdn', 'driveseed',[m
[32m+[m[32m    'gdflix', 'gdtot', 'filepress', 'vcloud', 'fsl', 'gofile', 'frdl', 'lplaygo',[m
[32m+[m[32m    'streamtape', 'savefiles', 'megaup', 'vinovo', 'voe', 'linksddr', 'uptobhai',[m
[32m+[m[32m    'vikingfile', 'buzzheavier',[m
     'dropgalaxy', 'pixeldrain', 'unknown', 'direct', 'stream', 'popcorn', 'nebula', 'nova',[m
     'torbox', 'realdebrid', 'alldebrid', 'debrid', 'mediafusion', 'comet',[m
     'knightcrawler', 'annatar', 'stremthru', 'jackettio', 'nuvio', 'showbox', 'addon',[m
[36m@@ -6514,6 +6536,102 @@[m [mdef _tokenize_clean_words(text: str) -> list[str]:[m
     return re.findall(r"[a-z0-9]+", normalized)[m
 [m
 [m
[32m+[m[32mdef _tokenize_strict_title_words(text: str) -> list[str]:[m
[32m+[m[32m    """Tokenize a direct-stream title with case folding and Unicode letters."""[m
[32m+[m[32m    if not text:[m
[32m+[m[32m        return [][m
[32m+[m[32m    unescaped = html.unescape(str(text))[m
[32m+[m[32m    nfkc = unicodedata.normalize("NFKC", unescaped).casefold()[m
[32m+[m[32m    decomposed = unicodedata.normalize("NFKD", nfkc)[m
[32m+[m[32m    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))[m
[32m+[m[32m    normalized = re.sub(r"[\'\u2018\u2019\u02bc\u2032`]", "", without_marks)[m
[32m+[m[32m    normalized = normalized.replace("&", " and ").replace("+", " plus ").replace("#", " sharp ")[m
[32m+[m[32m    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)[m
[32m+[m
[32m+[m
[32m+[m[32mdef strict_stream_title_matches(target_title, candidate_title, extra_ignored_words=None):[m
[32m+[m[32m    """Return True when a stream title starts with every requested title token in order.[m
[32m+[m
[32m+[m[32m    Punctuation is normalized consistently, so ``Kill Me, Heal Me`` matches[m
[32m+[m[32m    the same words separated by dots or hyphens. Meaningful ``+``, ``#``, and[m
[32m+[m[32m    ``&`` marks are retained as words to avoid collisions such as C++ and C#.[m
[32m+[m[32m    Provider labels and release metadata may precede the title; arbitrary title[m
[32m+[m[32m    words may not.[m
[32m+[m[32m    """[m
[32m+[m[32m    target_words = _tokenize_strict_title_words(target_title)[m
[32m+[m[32m    if not target_words or not candidate_title:[m
[32m+[m[32m        return False[m
[32m+[m
[32m+[m[32m    protected_words = set(target_words)[m
[32m+[m[32m    ignored_words = _PROVIDER_PREFIX_WORDS | _RELEASE_DESCRIPTOR_WORDS | MOVIE_STOP_WORDS[m
[32m+[m[32m    if extra_ignored_words:[m
[32m+[m[32m        if isinstance(extra_ignored_words, str):[m
[32m+[m[32m            extra_ignored_words = [extra_ignored_words][m
[32m+[m[32m        for word in extra_ignored_words:[m
[32m+[m[32m            ignored_words.update(_tokenize_strict_title_words(word))[m
[32m+[m[32m    ignored_words -= protected_words[m
[32m+[m
[32m+[m[32m    def _is_prefix_noise(text):[m
[32m+[m[32m        tokens = _tokenize_strict_title_words(text)[m
[32m+[m[32m        return bool(tokens) and all([m
[32m+[m[32m            _is_title_noise_token(token, ignored_words)[m
[32m+[m[32m            or re.fullmatch(r"(?:s|e)\d{1,3}|\d{1,2}x\d{1,3}", token)[m
[32m+[m[32m            for token in tokens[m
[32m+[m[32m        )[m
[32m+[m
[32m+[m[32m    raw = unicodedata.normalize("NFKC", html.unescape(str(candidate_title))).strip()[m
[32m+[m[32m    # Providers sometimes put the release name in one side of a UI separator.[m
[32m+[m[32m    # Check each segment independently, but only strip recognized provider or[m
[32m+[m[32m    # metadata prefixes from a segment.[m
[32m+[m[32m    segments = [raw][m
[32m+[m[32m    pipe_parts = [part.strip() for part in re.split(r"[|•]", raw) if part.strip()][m
[32m+[m[32m    prefix_is_metadata = True[m
[32m+[m[32m    for index, part in enumerate(pipe_parts):[m
[32m+[m[32m        if index == 0:[m
[32m+[m[32m            continue[m
[32m+[m[32m        prefix_is_metadata = prefix_is_metadata and _is_prefix_noise(pipe_parts[index - 1])[m
[32m+[m[32m        if prefix_is_metadata:[m
[32m+[m[32m            segments.append(part)[m
[32m+[m
[32m+[m[32m    for segment in segments:[m
[32m+[m[32m        text = segment[m
[32m+[m[32m        # Remove only bracketed tags that are known provider/quality metadata;[m
[32m+[m[32m        # a title itself may legitimately be enclosed in brackets.[m
[32m+[m[32m        while True:[m
[32m+[m[32m            tag_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", text)[m
[32m+[m[32m            if not tag_match or not _is_prefix_noise(tag_match.group(1)):[m
[32m+[m[32m                break[m
[32m+[m[32m            text = tag_match.group(2).strip()[m
[32m+[m
[32m+[m[32m        text = re.sub(r"^[^\w\[(]+", "", text, flags=re.UNICODE).strip()[m
[32m+[m[32m        for _ in range(5):[m
[32m+[m[32m            prefix_match = re.match(r"^([^:|•\-–—]+?)\s*(?:[:|•\-–—])\s*(.+)$", text)[m
[32m+[m[32m            if not prefix_match or not _is_prefix_noise(prefix_match.group(1)):[m
[32m+[m[32m                break[m
[32m+[m[32m            text = prefix_match.group(2).strip()[m
[32m+[m
[32m+[m[32m        words = _tokenize_strict_title_words(text)[m
[32m+[m[32m        while words and words[0] not in protected_words and ([m
[32m+[m[32m            _is_title_noise_token(words[0], ignored_words)[m
[32m+[m[32m            or re.fullmatch(r"(?:s|e)\d{1,3}|\d{1,2}x\d{1,3}", words[0])[m
[32m+[m[32m        ):[m
[32m+[m[32m            words.pop(0)[m
[32m+[m[32m        joined_target = "".join(target_words)[m
[32m+[m[32m        joined_prefix = False[m
[32m+[m[32m        for token_count in range(1, len(words) + 1):[m
[32m+[m[32m            if "".join(words[:token_count]) == joined_target:[m
[32m+[m[32m                joined_prefix = True[m
[32m+[m[32m                break[m
[32m+[m[32m        if ([m
[32m+[m[32m            words[:len(target_words)] == target_words[m
[32m+[m[32m            or joined_prefix[m
[32m+[m[32m            or (words and re.fullmatch(re.escape(joined_target) + r"\d+", words[0]))[m
[32m+[m[32m        ):[m
[32m+[m[32m            return True[m
[32m+[m
[32m+[m[32m    return False[m
[32m+[m
[32m+[m
 def _is_title_noise_token(token: str, ignored_words: set[str]) -> bool:[m
     """Return True if token is a pure provider/descriptor/numeric-unit metadata tag."""[m
     if not token:[m
[36m@@ -6779,6 +6897,88 @@[m [mdef is_direct_stream(stream):[m
     return False[m
 [m
 [m
[32m+[m[32mdef strict_stream_title_mismatch(stream, target_title):[m
[32m+[m[32m    """Return MATCH_MISMATCH when a direct stream exposes a different title.[m
[32m+[m
[32m+[m[32m    Opaque URLs and provider/quality-only labels remain unknown. Torrent streams[m
[32m+[m[32m    are deliberately excluded so their existing matching and retrieval path is[m
[32m+[m[32m    unchanged.[m
[32m+[m[32m    """[m
[32m+[m[32m    if not isinstance(stream, dict) or not target_title or is_torrent_stream(stream):[m
[32m+[m[32m        return MATCH_UNKNOWN[m
[32m+[m
[32m+[m[32m    provider_words = set()[m
[32m+[m[32m    addon_names = stream.get("addon_names") or [][m
[32m+[m[32m    if isinstance(addon_names, str):[m
[32m+[m[32m        addon_names = [addon_names][m
[32m+[m[32m    for value in (stream.get("addon_name"), *addon_names):[m
[32m+[m[32m        if isinstance(value, str) and value:[m
[32m+[m[32m            provider_words.update(_tokenize_strict_title_words(value))[m
[32m+[m[32m    if not provider_words and isinstance(stream.get("name"), str):[m
[32m+[m[32m        # Some Stremio streams use `name` only for a provider/quality label.[m
[32m+[m[32m        # Prefer explicit provider fields when present, since custom scrapers[m
[32m+[m[32m        # also use `name` for the actual movie or episode title.[m
[32m+[m[32m        provider_words.update(_tokenize_strict_title_words(stream["name"]))[m
[32m+[m[32m    target_words = set(_tokenize_strict_title_words(target_title))[m
[32m+[m[32m    provider_words -= target_words[m
[32m+[m
[32m+[m[32m    candidates = [][m
[32m+[m[32m    url = stream.get("url") or stream.get("externalUrl") or stream.get("magnet")[m
[32m+[m[32m    url_filename = get_stream_url_filename(url)[m
[32m+[m[32m    if url_filename and not is_opaque_identifier(url_filename):[m
[32m+[m[32m        candidates.append((url_filename, True))[m
[32m+[m
[32m+[m[32m    hints = stream.get("behaviorHints") or {}[m
[32m+[m[32m    if isinstance(hints, dict):[m
[32m+[m[32m        for key in ("filename", "videoFilename"):[m
[32m+[m[32m            value = hints.get(key)[m
[32m+[m[32m            if isinstance(value, str) and value.strip():[m
[32m+[m[32m                candidates.append((value, True))[m
[32m+[m[32m    if isinstance(stream.get("filename"), str) and stream.get("filename").strip():[m
[32m+[m[32m        candidates.append((stream.get("filename"), True))[m
[32m+[m[32m    for field_name in ("stream_title", "title", "description", "name"):[m
[32m+[m[32m        value = stream.get(field_name)[m
[32m+[m[32m        if isinstance(value, str) and value.strip():[m
[32m+[m[32m            candidates.append((value, False))[m
[32m+[m
[32m+[m[32m    ignored_words = ([m
[32m+[m[32m        _PROVIDER_PREFIX_WORDS[m
[32m+[m[32m        | _RELEASE_DESCRIPTOR_WORDS[m
[32m+[m[32m        | MOVIE_STOP_WORDS[m
[32m+[m[32m        | provider_words[m
[32m+[m[32m    ) - target_words[m
[32m+[m[32m    has_title_evidence = False[m
[32m+[m[32m    checked_candidates = set()[m
[32m+[m[32m    matched_title = False[m
[32m+[m[32m    for candidate, authoritative in candidates:[m
[32m+[m[32m        if candidate in checked_candidates or is_opaque_identifier(candidate):[m
[32m+[m[32m            continue[m
[32m+[m[32m        checked_candidates.add(candidate)[m
[32m+[m
[32m+[m[32m        lines = [line.strip() for line in candidate.splitlines() if line.strip()][m
[32m+[m[32m        title_forms = lines[:][m
[32m+[m[32m        if len(lines) >= 2:[m
[32m+[m[32m            title_forms.append(" ".join(lines[:2]))[m
[32m+[m[32m        for form in title_forms:[m
[32m+[m[32m            if strict_stream_title_matches(target_title, form, extra_ignored_words=provider_words):[m
[32m+[m[32m                matched_title = True[m
[32m+[m[32m                continue[m
[32m+[m[32m            words = _tokenize_strict_title_words(form)[m
[32m+[m[32m            core_words = [[m
[32m+[m[32m                word for word in words[m
[32m+[m[32m                if not _is_title_noise_token(word, ignored_words)[m
[32m+[m[32m                and not re.fullmatch(r"(?:s|e)\d{1,3}|\d{1,2}x\d{1,3}", word)[m
[32m+[m[32m            ][m
[32m+[m[32m            if core_words:[m
[32m+[m[32m                has_title_evidence = True[m
[32m+[m[32m                if authoritative:[m
[32m+[m[32m                    return MATCH_MISMATCH[m
[32m+[m
[32m+[m[32m    if matched_title:[m
[32m+[m[32m        return MATCH_UNKNOWN[m
[32m+[m[32m    return MATCH_MISMATCH if has_title_evidence else MATCH_UNKNOWN[m
[32m+[m
[32m+[m
 def is_youtube_stream(stream):[m
     """Determine if a stream dictionary represents a YouTube video/stream."""[m
     if not isinstance(stream, dict):[m
[36m@@ -8037,7 +8237,10 @@[m [mdef get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None[m
     expected_year = str(expected_year or "").strip()[:4][m
     if len(expected_year) != 4 or not expected_year.isdigit():[m
         expected_year = ""[m
[31m-    is_movie_request = str(media_type or "movie").strip().lower() not in ("series", "show", "tvshow", "tv_show", "anime", "tv")[m
[32m+[m[32m    requested_media_type = str(media_type or "movie").strip().lower()[m
[32m+[m[32m    is_non_anime_media_request = requested_media_type in ([m
[32m+[m[32m        "movie", "series", "show", "tvshow", "tv_show", "tv"[m
[32m+[m[32m    )[m
 [m
     if not title and imdb_id:[m
         try:[m
[36m@@ -8059,6 +8262,12 @@[m [mdef get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None[m
             if not isinstance(stream, dict):[m
                 accepted.append(stream)[m
                 continue[m
[32m+[m[32m            if ([m
[32m+[m[32m                not is_torrent_stream(stream)[m
[32m+[m[32m                and strict_stream_title_mismatch(stream, title) == MATCH_MISMATCH[m
[32m+[m[32m            ):[m
[32m+[m[32m                print(f"[STREAM DEBUG] discarded direct stream without exact title sequence title={title!r} url={(stream.get('url') or '')[:80]}")[m
[32m+[m[32m                continue[m
             if season is not None and episode is not None:[m
                 if match_stream_to_episode(stream, season, episode, ep_title=ep_title, series_title=title) == MATCH_MISMATCH:[m
                     print(f"[STREAM DEBUG] discarded episode/movie-mismatched stream title={title!r} url={(stream.get('url') or '')[:80]}")[m
[36m@@ -8098,27 +8307,60 @@[m [mdef get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None[m
             target_custom_route = cand[m
             break[m
 [m
[31m-    initial_custom_streams = [][m
[31m-    clean_source_scope_pre = str(source_scope).strip().lower() if source_scope else None[m
[31m-    if target_custom_route and clean_source_scope_pre == "direct":[m
[31m-        from . import custom_plugins[m
[31m-        print(f"[STREAM DEBUG] Custom plugin immediate route lookup starting: {target_custom_route!r} s={season} e={episode}")[m
[32m+[m[32m    # A custom catalog item already has a provider-authored title and route.[m
[32m+[m[32m    # Prefer that title over a possibly ambiguous TMDB title for stream[m
[32m+[m[32m    # matching, especially for short films and catalog-only releases.[m
[32m+[m[32m    if target_custom_route:[m
[32m+[m[32m        route_catalog_title = str(catalog_title or "").strip()[m
[32m+[m[32m        if not route_catalog_title:[m
[32m+[m[32m            route_data = target_custom_route.split(":", 1)[1] if ":" in target_custom_route else ""[m
[32m+[m[32m            route_path = urllib.parse.urlsplit(route_data).path[m
[32m+[m[32m            route_slug = route_path.rstrip("/").split("/")[-1][m
[32m+[m[32m            route_catalog_title = route_slug.replace("-", " ").replace("_", " ").strip()[m
[32m+[m[32m        if route_catalog_title:[m
[32m+[m[32m            clean_route_title, route_year = clean_media_title(route_catalog_title)[m
[32m+[m[32m            if clean_route_title:[m
[32m+[m[32m                title = clean_route_title[m
[32m+[m[32m                if route_year:[m
[32m+[m[32m                    expected_year = str(route_year)[m
[32m+[m[32m                print([m
[32m+[m[32m                    f"[STREAM DEBUG] custom route catalog title selected "[m
[32m+[m[32m                    f"title={title!r} year={expected_year!r} route={target_custom_route[:120]!r}"[m
[32m+[m[32m                )[m
[32m+[m
[32m+[m[32m    from . import custom_plugins[m
[32m+[m[32m    matching_custom_route_scraper = None[m
[32m+[m[32m    if target_custom_route:[m
         if target_custom_route.lstrip().startswith("{") and "movieid" in target_custom_route.lower():[m
[31m-            prefix = "castletv"[m
[32m+[m[32m            route_provider_prefix = "castletv"[m
         else:[m
[31m-            prefix = target_custom_route.split(":", 1)[0].lower().replace("custom.", "")[m
[31m-        scrapers = ([m
[32m+[m[32m            route_provider_prefix = target_custom_route.split(":", 1)[0].lower().replace("custom.", "")[m
[32m+[m[32m        route_scrapers = ([m
             custom_plugins.get_enabled_scrapers_for_type(media_type)[m
             if custom_plugins.is_custom_plugins_enabled()[m
             else [][m
         )[m
[31m-        matching_scraper = None[m
[31m-        for s in scrapers:[m
[31m-            s_name = (getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else "")).lower().replace(" ", "")[m
[31m-            s_id = (getattr(s, "id", None) or (s.get("id") if isinstance(s, dict) else "")).lower().replace("custom.", "")[m
[31m-            if prefix in (s_name, s_id) or s_name in prefix or s_id in prefix:[m
[31m-                matching_scraper = s[m
[32m+[m[32m        for scraper in route_scrapers:[m
[32m+[m[32m            scraper_name = (getattr(scraper, "name", None) or "").lower().replace(" ", "")[m
[32m+[m[32m            scraper_id = (getattr(scraper, "id", None) or "").lower().replace("custom.", "")[m
[32m+[m[32m            if ([m
[32m+[m[32m                route_provider_prefix in (scraper_name, scraper_id)[m
[32m+[m[32m                or scraper_name in route_provider_prefix[m
[32m+[m[32m                or scraper_id in route_provider_prefix[m
[32m+[m[32m            ):[m
[32m+[m[32m                matching_custom_route_scraper = scraper[m
                 break[m
[32m+[m[32m        if str(source_scope or "").strip().lower() != "torrent":[m
[32m+[m[32m            provider_scope = ([m
[32m+[m[32m                getattr(matching_custom_route_scraper, "name", None)[m
[32m+[m[32m                or route_provider_prefix[m
[32m+[m[32m            )[m
[32m+[m
[32m+[m[32m    initial_custom_streams = [][m
[32m+[m[32m    clean_source_scope_pre = str(source_scope).strip().lower() if source_scope else None[m
[32m+[m[32m    if target_custom_route and clean_source_scope_pre == "direct":[m
[32m+[m[32m        print(f"[STREAM DEBUG] Custom plugin immediate route lookup starting: {target_custom_route!r} s={season} e={episode}")[m
[32m+[m[32m        matching_scraper = matching_custom_route_scraper[m
         if matching_scraper:[m
             try:[m
                 c_streams = custom_plugins.execute_custom_scraper([m
[36m@@ -8132,6 +8374,11 @@[m [mdef get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None[m
                     tmdb_id=known_tmdb_id,[m
                     timeout=12.0[m
                 )[m
[32m+[m[32m                if c_streams:[m
[32m+[m[32m                    c_streams = [[m
[32m+[m[32m                        stream for stream in c_streams[m
[32m+[m[32m                        if strict_stream_title_mismatch(stream, title) != MATCH_MISMATCH[m
[32m+[m[32m                    ][m
                 if c_streams:[m
                     print(f"[STREAM DEBUG] Custom plugin immediate route lookup returned {len(c_streams)} stream(s)")[m
                     initial_custom_streams = list(c_streams)[m
[36m@@ -8323,6 +8570,47 @@[m [mdef get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None[m
         cached = [s for s in cached if not _has_wrong_tmdb(s)][m
         cache_candidates = [s for s in cache_candidates if not _has_wrong_tmdb(s)][m
 [m
[32m+[m[32m    if is_non_anime_media_request and source_scope != "torrent":[m
[32m+[m[32m        cached_type_scrapers = [[m
[32m+[m[32m            scraper for scraper in plugins.get_all_scrapers()[m
[32m+[m[32m            if plugins.scraper_is_anime_only(scraper)[m
[32m+[m[32m        ][m
[32m+[m[32m        anime_only_ids = {str(scraper.get("id") or "") for scraper in cached_type_scrapers}[m
[32m+[m[32m        anime_only_provider_names = {[m
[32m+[m[32m            str(name).strip().casefold()[m
[32m+[m[32m            for scraper in cached_type_scrapers[m
[32m+[m[32m            for name in (scraper.get("name"), scraper.get("raw_id"))[m
[32m+[m[32m            if name[m
[32m+[m[32m        }[m
[32m+[m[32m        if anime_only_ids:[m
[32m+[m[32m            def _keep_non_anime_cache_stream(stream):[m
[32m+[m[32m                if not isinstance(stream, dict) or is_torrent_stream(stream):[m
[32m+[m[32m                    return True[m
[32m+[m[32m                scraper_id = str(stream.get("plugin_scraper_id") or "")[m
[32m+[m[32m                if stream.get("is_nuvio_plugin") and scraper_id in anime_only_ids:[m
[32m+[m[32m                    return False[m
[32m+[m[32m                cached_names = stream.get("addon_names") or [][m
[32m+[m[32m                if isinstance(cached_names, str):[m
[32m+[m[32m                    cached_names = [cached_names][m
[32m+[m[32m                else:[m
[32m+[m[32m                    cached_names = list(cached_names)[m
[32m+[m[32m                if stream.get("addon_name"):[m
[32m+[m[32m                    cached_names.append(stream.get("addon_name"))[m
[32m+[m[32m                return not any([m
[32m+[m[32m                    str(name).strip().casefold() in anime_only_provider_names[m
[32m+[m[32m                    for name in cached_names[m
[32m+[m[32m                    if name[m
[32m+[m[32m                )[m
[32m+[m
[32m+[m[32m            before_cache_type_filter = len(cached)[m
[32m+[m[32m            cached = [s for s in cached if _keep_non_anime_cache_stream(s)][m
[32m+[m[32m            cache_candidates = [s for s in cache_candidates if _keep_non_anime_cache_stream(s)][m
[32m+[m[32m            if len(cached) != before_cache_type_filter:[m
[32m+[m[32m                print([m
[32m+[m[32m                    f"[STREAM DEBUG] removed cached anime-only Nuvio direct streams "[m
[32m+[m[32m                    f"count={before_cache_type_filter - len(cached)}"[m
[32m+[m[32m                )[m
[32m+[m
     cached = _filter_mismatched_movie_streams(cached)[m
     cache_candidates = _filter_mismatched_movie_streams(cache_candidates)[m
 [m
[36m@@ -8380,6 +8668,23 @@[m [mdef get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None[m
         plugin_scrapers = [s for s in plugin_scrapers if plugins.is_torrent_capable_scraper(s)][m
     elif source_scope == "direct":[m
         plugin_scrapers = [s for s in plugin_scrapers if not plugins.is_torrent_capable_scraper(s)][m
[32m+[m[32m    if is_non_anime_media_request and source_scope != "torrent":[m
[32m+[m[32m        anime_only_scrapers = [s for s in plugin_scrapers if plugins.scraper_is_anime_only(s)][m
[32m+[m[32m        if source_scope == "direct":[m
[32m+[m[32m            plugin_scrapers = [s for s in plugin_scrapers if not plugins.scraper_is_anime_only(s)][m
[32m+[m[32m        else:[m
[32m+[m[32m            # A mixed search still preserves explicitly torrent-capable anime[m
[32m+[m[32m            # scrapers, but they cannot contribute direct streams to a regular[m
[32m+[m[32m            # TV-series request.[m
[32m+[m[32m            plugin_scrapers = [[m
[32m+[m[32m                s for s in plugin_scrapers[m
[32m+[m[32m                if not plugins.scraper_is_anime_only(s) or plugins.is_torrent_capable_scraper(s)[m
[32m+[m[32m            ][m
[32m+[m[32m        if anime_only_scrapers:[m
[32m+[m[32m            print([m
[32m+[m[32m                f"[STREAM DEBUG] filtered anime-only Nuvio scrapers for non-anime request "[m
[32m+[m[32m                f"request count={len(anime_only_scrapers)} source_scope={source_scope!r}"[m
[32m+[m[32m            )[m
     if provider_scope:[m
         plugin_scrapers = [[m
             s for s in plugin_scrapers[m
[36m@@ -8931,6 +9236,18 @@[m [mdef get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None[m
                             addon_orig_info = future_to_addon.get(future, (None, None))[m
                             addon_obj = addon_orig_info[0] if addon_orig_info else None[m
                             addon_name, streams = future.result()[m
[32m+[m[32m                            if ([m
[32m+[m[32m                                isinstance(addon_obj, dict)[m
[32m+[m[32m                                and is_non_anime_media_request[m
[32m+[m[32m                                and plugins.scraper_is_anime_only(addon_obj)[m
[32m+[m[32m                            ):[m
[32m+[m[32m                                before_type_filter = len(streams or [])[m
[32m+[m[32m                                streams = [s for s in (streams or []) if is_torrent_stream(s)][m
[32m+[m[32m                                if len(streams) != before_type_filter:[m
[32m+[m[32m                                    print([m
[32m+[m[32m                                        f"[STREAM DEBUG] removed direct output from anime-only Nuvio scraper "[m
[32m+[m[32m                                        f"provider={addon_name!r} count={before_type_filter - len(streams)}"[m
[32m+[m[32m                                    )[m
                             if streams and addon_obj and addon_obj.get("id"):[m
                                 streams = _filter_mismatched_movie_streams(streams, allow_html_probe=True)[m
                             else:[m
[1mdiff --git a/src/cloudstream.py b/src/cloudstream.py[m
[1mindex 6ceb218..5a02278 100644[m
[1m--- a/src/cloudstream.py[m
[1m+++ b/src/cloudstream.py[m
[36m@@ -25,6 +25,7 @@[m [mimport codecs[m
 import base64[m
 import threading[m
 import html as html_lib[m
[32m+[m[32mimport unicodedata[m
 import urllib.request[m
 import urllib.error[m
 import urllib.parse[m
[36m@@ -3451,34 +3452,8 @@[m [m_RELEASE_TAG_WORDS = {[m
 [m
 [m
 def matches_all_query_terms(query, candidate_title):[m
[31m-    """[m
[31m-    Returns True if ALL significant query terms are present in candidate_title[m
[31m-    (case-insensitive, equivalent to piping multiple grep -i commands:[m
[31m-    e.g. query 'American Greed' requires candidate to match 'American' AND 'Greed').[m
[31m-    """[m
[31m-    if not query or not candidate_title:[m
[31m-        return False[m
[31m-    q_clean, _ = clean_cloudstream_title(query)[m
[31m-    c_clean, _ = clean_cloudstream_title(candidate_title)[m
[31m-[m
[31m-    raw_q_words = re.findall(r"\w+", (q_clean or query).lower())[m
[31m-    q_words = [w for w in raw_q_words if w not in _STOP_WORDS and len(w) >= 2][m
[31m-    if not q_words:[m
[31m-        q_words = [w for w in raw_q_words if len(w) >= 1][m
[31m-    if not q_words:[m
[31m-        return False[m
[31m-[m
[31m-    c_text = (c_clean or candidate_title).lower()[m
[31m-    c_norm = re.sub(r"[^\w\s]", " ", c_text)[m
[31m-[m
[31m-    for word in q_words:[m
[31m-        pattern = r"\b" + re.escape(word) + r"\b"[m
[31m-        if not re.search(pattern, c_norm):[m
[31m-            if len(word) >= 4 and word in c_norm:[m
[31m-                continue[m
[31m-            return False[m
[31m-[m
[31m-    return True[m
[32m+[m[32m    """Compatibility wrapper for exact ordered CloudStream title matching."""[m
[32m+[m[32m    return _is_strict_title_match(query, candidate_title)[m
 [m
 [m
 def _is_strict_title_match(query, candidate):[m
[36m@@ -3486,69 +3461,53 @@[m [mdef _is_strict_title_match(query, candidate):[m
         return False[m
     q_clean, _ = clean_cloudstream_title(query)[m
     c_clean, _ = clean_cloudstream_title(candidate)[m
[31m-[m
[31m-    q_norm = re.sub(r"[^\w\s]", "", (q_clean or query).lower()).strip()[m
[31m-    c_norm = re.sub(r"[^\w\s]", "", (c_clean or candidate).lower()).strip()[m
[31m-    if not q_norm or not c_norm:[m
[32m+[m[32m    def _tokens(value):[m
[32m+[m[32m        normalized = unicodedata.normalize("NFKC", html_lib.unescape(str(value or ""))).casefold()[m
[32m+[m[32m        normalized = unicodedata.normalize("NFKD", normalized)[m
[32m+[m[32m        normalized = "".join(char for char in normalized if not unicodedata.combining(char))[m
[32m+[m[32m        normalized = normalized.replace("&", " and ").replace("+", " plus ").replace("#", " sharp ")[m
[32m+[m[32m        return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)[m
[32m+[m
[32m+[m[32m    query_words = _tokens(q_clean or query)[m
[32m+[m[32m    if not query_words:[m
         return False[m
[31m-    if q_norm == c_norm:[m
[31m-        return True[m
 [m
[31m-    for text in (candidate, c_clean):[m
[31m-        if not text:[m
[32m+[m[32m    # Catalog titles may carry only release metadata after the exact title.[m
[32m+[m[32m    # Keep all title words, including short words such as "me" and "the".[m
[32m+[m[32m    allowed_suffix_words = _RELEASE_TAG_WORDS | {[m
[32m+[m[32m        "tv", "drama", "kdrama", "series", "show", "season", "episode", "ep",[m
[32m+[m[32m        "complete", "all", "episodes", "part", "vol", "volume", "collection",[m
[32m+[m[32m    }[m
[32m+[m[32m    candidates = [candidate, c_clean][m
[32m+[m[32m    for raw_candidate in candidates:[m
[32m+[m[32m        words = _tokens(raw_candidate)[m
[32m+[m[32m        if not words:[m
             continue[m
[31m-        for sep in (" - ", " : ", " | ", " – ", " — "):[m
[31m-            if sep in text:[m
[31m-                prefix = text.split(sep, 1)[0].strip()[m
[31m-                p_clean, _ = clean_cloudstream_title(prefix)[m
[31m-                prefix_norm = re.sub(r"[^\w\s]", "", (p_clean or prefix).lower()).strip()[m
[31m-                if prefix_norm == q_norm:[m
[31m-                    return True[m
[31m-[m
[31m-    for text in (query, q_clean):[m
[31m-        if not text:[m
[32m+[m[32m        prefix_length = None[m
[32m+[m[32m        if words[:len(query_words)] == query_words:[m
[32m+[m[32m            prefix_length = len(query_words)[m
[32m+[m[32m        else:[m
[32m+[m[32m            joined_query = "".join(query_words)[m
[32m+[m[32m            for count in range(1, len(words) + 1):[m
[32m+[m[32m                if "".join(words[:count]) == joined_query:[m
[32m+[m[32m                    prefix_length = count[m
[32m+[m[32m                    break[m
[32m+[m[32m        if prefix_length is None:[m
             continue[m
[31m-        for sep in (" - ", " : ", " | ", " – ", " — "):[m
[31m-            if sep in text:[m
[31m-                q_prefix = text.split(sep, 1)[0].strip()[m
[31m-                qp_clean, _ = clean_cloudstream_title(q_prefix)[m
[31m-                qp_norm = re.sub(r"[^\w\s]", "", (qp_clean or q_prefix).lower()).strip()[m
[31m-                if qp_norm == c_norm:[m
[31m-                    return True[m
[31m-[m
[31m-    q_words = [w for w in q_norm.split() if w not in _STOP_WORDS] or q_norm.split()[m
[31m-    c_words = [w for w in c_norm.split() if w not in _STOP_WORDS] or c_norm.split()[m
[31m-[m
[31m-    q_filtered = [w for w in q_words if w not in _RELEASE_TAG_WORDS] or q_words[m
[31m-    c_filtered = [w for w in c_words if w not in _RELEASE_TAG_WORDS] or c_words[m
[31m-[m
[31m-    q_set = set(q_filtered)[m
[31m-    c_set = set(c_filtered)[m
[31m-[m
[31m-    if not matches_all_query_terms(query, candidate):[m
[31m-        return False[m
[31m-[m
[31m-    if q_set == c_set:[m
[31m-        return True[m
[31m-[m
[31m-    overlap = q_set.intersection(c_set)[m
[31m-    if not overlap:[m
[31m-        return False[m
[31m-[m
[31m-    if len(q_set) == 1 or len(c_set) == 1:[m
[31m-        if q_set == c_set:[m
[32m+[m[32m        suffix = words[prefix_length:][m
[32m+[m[32m        if all([m
[32m+[m[32m            word in allowed_suffix_words[m
[32m+[m[32m            or re.fullmatch(r"(?:18|19|20)\d{2}", word)[m
[32m+[m[32m            or re.fullmatch(r"(?:s|e)\d{1,3}|\d{1,2}x\d{1,3}", word)[m
[32m+[m[32m            or ([m
[32m+[m[32m                word.isdigit()[m
[32m+[m[32m                and index > 0[m
[32m+[m[32m                and suffix[index - 1] in {"season", "episode", "episodes", "ep", "part", "vol", "volume"}[m
[32m+[m[32m            )[m
[32m+[m[32m            for index, word in enumerate(suffix)[m
[32m+[m[32m        ):[m
             return True[m
[31m-        if q_set.issubset(c_set):[m
[31m-            # If the raw normalized query had multiple words (e.g. 'the outsider')[m
[31m-            # and the whole phrase appears in candidate:[m
[31m-            if len(q_norm.split()) >= 2 and re.search(r"\b" + re.escape(q_norm) + r"\b", c_norm):[m
[31m-                return True[m
[31m-        return False[m
[31m-[m
[31m-    q_ratio = len(overlap) / len(q_set)[m
[31m-    c_ratio = len(overlap) / len(c_set)[m
[31m-[m
[31m-    return q_ratio >= 0.75 and c_ratio >= 0.75[m
[32m+[m[32m    return False[m
 [m
 [m
 CLOUDSTREAM_URL_SKIP_DOMAINS = {[m
[1mdiff --git a/src/plugins.py b/src/plugins.py[m
[1mindex b131bb2..4dbdb5e 100644[m
[1m--- a/src/plugins.py[m
[1m+++ b/src/plugins.py[m
[36m@@ -477,6 +477,38 @@[m [mdef scraper_supports_type(scraper, media_type):[m
     return normalize_media_type(media_type) in supported[m
 [m
 [m
[32m+[m[32mdef scraper_is_anime_only(scraper):[m
[32m+[m[32m    """Identify anime-focused scrapers that should not serve general TV series."""[m
[32m+[m[32m    if not isinstance(scraper, dict):[m
[32m+[m[32m        return False[m
[32m+[m[32m    raw_types = scraper.get("supported_types") or ["movie", "tv"][m
[32m+[m[32m    if isinstance(raw_types, str):[m
[32m+[m[32m        raw_types = [raw_types][m
[32m+[m[32m    types = {str(value).strip().casefold() for value in raw_types if value}[m
[32m+[m[32m    if "all" in types:[m
[32m+[m[32m        return False[m
[32m+[m[32m    anime_types = {"anime", "ova"}[m
[32m+[m[32m    general_series_types = {"series", "tv", "show", "tvshow", "tv_show"}[m
[32m+[m[32m    if types & anime_types and not types & general_series_types:[m
[32m+[m[32m        return True[m
[32m+[m
[32m+[m[32m    identity = " ".join([m
[32m+[m[32m        str(scraper.get(key) or "")[m
[32m+[m[32m        for key in ("name", "raw_id", "id", "description")[m
[32m+[m[32m    ).casefold()[m
[32m+[m[32m    anime_identity_markers = ([m
[32m+[m[32m        "anime", "anilist", "myanimelist", "hianime", "animepahe", "animekai",[m
[32m+[m[32m    )[m
[32m+[m[32m    general_scope_markers = ([m
[32m+[m[32m        "all media", "all genres", "general tv", "general series", "movies and tv",[m
[32m+[m[32m        "movies, series", "all movies and series",[m
[32m+[m[32m    )[m
[32m+[m[32m    return ([m
[32m+[m[32m        any(marker in identity for marker in anime_identity_markers)[m
[32m+[m[32m        and not any(marker in identity for marker in general_scope_markers)[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m
 def get_enabled_scrapers_for_type(media_type):[m
     """Return all enabled scrapers that support the given media type."""[m
     if not is_plugins_enabled():[m
[36m@@ -1221,21 +1253,57 @@[m [mdef execute_scraper(scraper, tmdb_id, media_type, season=None, episode=None, tim[m
 [m
         # Find the last valid JSON line.[m
         selected_title = _extract_scraper_selected_title(lines)[m
[32m+[m
[32m+[m[32m        def _raw_items_include_torrent(items):[m
[32m+[m[32m            for item in items:[m
[32m+[m[32m                if not isinstance(item, dict):[m
[32m+[m[32m                    continue[m
[32m+[m[32m                raw_url = item.get("url")[m
[32m+[m[32m                if isinstance(raw_url, dict):[m
[32m+[m[32m                    raw_url = raw_url.get("url")[m
[32m+[m[32m                url_text = str(raw_url or "").strip().lower()[m
[32m+[m[32m                if ([m
[32m+[m[32m                    item.get("infoHash")[m
[32m+[m[32m                    or item.get("info_hash")[m
[32m+[m[32m                    or item.get("magnet")[m
[32m+[m[32m                    or str(item.get("stream_type") or "").lower() == "torrent"[m
[32m+[m[32m                    or url_text.startswith("magnet:")[m
[32m+[m[32m                    or url_text.split("?", 1)[0].endswith(".torrent")[m
[32m+[m[32m                    or (item.get("is_http") is False and (item.get("seeders") or item.get("seeds")))[m
[32m+[m[32m                ):[m
[32m+[m[32m                    return True[m
[32m+[m[32m            return False[m
[32m+[m
         for line in reversed(lines):[m
             if (line.startswith("[") and line.endswith("]")) or (line.startswith("{") and line.endswith("}")):[m
                 try:[m
                     items = _unwrap_items(json.loads(line))[m
                     if items is not None:[m
[31m-                        if expected_title and selected_title and items:[m
[32m+[m[32m                        if ([m
[32m+[m[32m                            expected_title[m
[32m+[m[32m                            and selected_title[m
[32m+[m[32m                            and items[m
[32m+[m[32m                            and not is_torrent_capable_scraper(scraper)[m
[32m+[m[32m                        ):[m
                             from . import api[m
[31m-                            if api.check_movie_title_mismatch(expected_title, selected_title, require_scene_boundary=False):[m
[32m+[m[32m                            selected_title_matches = api.strict_stream_title_matches([m
[32m+[m[32m                                expected_title,[m
[32m+[m[32m                                selected_title,[m
[32m+[m[32m                                extra_ignored_words={[m
[32m+[m[32m                                    get_scraper_provider_name(scraper),[m
[32m+[m[32m                                    scraper.get("name") or "",[m
[32m+[m[32m                                },[m
[32m+[m[32m                            )[m
[32m+[m[32m                            if not selected_title_matches:[m
                                 logger.warning([m
[31m-                                    "Discarding mismatched results from scraper %r: selected %r for expected %r",[m
[32m+[m[32m                                    "Discarding mismatched direct results from scraper %r: selected %r for expected %r",[m
                                     scraper.get("name"),[m
                                     selected_title,[m
                                     expected_title,[m
                                 )[m
[31m-                                return _err(SCRAPER_ERROR_EMPTY)[m
[32m+[m[32m                                items = [item for item in items if _raw_items_include_torrent([item])][m
[32m+[m[32m                                if not items:[m
[32m+[m[32m                                    return _err(SCRAPER_ERROR_EMPTY)[m
                         formatted = _format_scraper_results(items, scraper, queried_tmdb_id=tmdb_id)[m
                         _record_scraper_success([m
                             scraper_id,[m
