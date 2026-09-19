================================================================================
POPCORNBOX - AI AGENT ARCHITECTURE & PERFORMANCE GUIDELINES
================================================================================

This document provides mandatory architecture, design patterns, and performance
standards for AI coding assistants working on the PopcornBox codebase.

--------------------------------------------------------------------------------
1. CORE PERFORMANCE PHILOSOPHY
--------------------------------------------------------------------------------
- PopcornBox must ALWAYS be buttery smooth, fluid, and instantly responsive.
- ZERO UI freezes, ZERO hangs, ZERO lag, and ZERO CPU spikes.
- The GTK main thread must remain free to process events and render at 60+ FPS.
- Users often have 60+ addons installed, resulting in hundreds of catalogs and
  thousands of potential items. Never assume a small dataset.

--------------------------------------------------------------------------------
2. UI THREAD HYGIENE & ASYNCHRONOUS WORK
--------------------------------------------------------------------------------
- NEVER perform network requests, database queries, disk I/O, or heavy data
  processing on the GTK main thread.
- ALWAYS offload data fetching, API communication, and manifest parsing to
  background daemon threads (`threading.Thread(..., daemon=True)`).
- Use `GLib.idle_add()` ONLY to deliver prepared results to the UI in small,
  non-blocking chunks. Do not run heavy loops or computations inside idle callbacks.

--------------------------------------------------------------------------------
3. MENU BUTTONS & GTK MENU MODELS
--------------------------------------------------------------------------------
- DO NOT POPULATE MENUS ON CLICK:
  Attaching or rebuilding a `Gio.Menu` or `GtkPopoverMenu` on click (e.g., inside
  a `notify::active` callback) causes GTK to synchronously instantiate widgets,
  parse CSS styling, and recalculate layouts during the click event. This results
  in an annoying lag, visible freeze, and high CPU spike right when the user clicks.

- PRE-ATTACH MODELS AHEAD OF TIME:
  Construct `Gio.Menu` models in a background worker thread. Once built, attach
  them to `GtkMenuButton` widgets via `GLib.idle_add()` across idle slices BEFORE
  the user interacts with them. When the user clicks the menu button, the menu
  is already built and attached, opening INSTANTLY on the first click.

- PREVENT MENU BLOAT (KEEP MENU HIERARCHIES SHALLOW):
  - Do NOT generate deeply nested submenus with hundreds or thousands of leaves
    (e.g., nesting 20-50 genres under every single catalog in header menus).
  - PopcornBox already provides dedicated header dropdowns (e.g., `genre_dropdown`)
    in the content grid view. Category menus (Movies, Series, Anime) must only
    provide direct navigation: Addon -> Catalogs (e.g., Popular, Top, New).
  - Streamlining menus from ~1,400 items down to ~200 items reduces widget
    instantiation overhead by 85-95% and cuts menu build time to <1ms.

- HANDLE ACTIVE STATE SAFELY:
  If a background update finishes while a user currently has a menu popover open,
  do NOT replace the model immediately (which tears down the popover). Defer
  the update until `notify::active` indicates the popover has closed.

--------------------------------------------------------------------------------
4. FAST STARTUP & INSTANT TAB SWITCHING
--------------------------------------------------------------------------------
- FAST LAUNCH:
  Startup must take milliseconds. During window initialization, assign lightweight
  placeholder models (`★ All Catalogs`) immediately, and defer full menu builds
  using `GLib.timeout_add(500, self._ensure_all_menus_built)`.
- INSTANT TAB SWITCHING:
  Switching between linked buttons (Discover, Movies, Series, Anime) must be
  instantaneous. Tab switch handlers (`on_switch_movies`, etc.) must simply change
  stack pages and trigger discover page refreshes; they must never synchronously
  construct or re-attach menu models.

--------------------------------------------------------------------------------
5. STREAM & IMAGE HANDLING
--------------------------------------------------------------------------------
- Cancel pending image downloads (`cancel_pending_image_downloads()`) whenever
  switching tabs or navigating views to avoid background network/CPU waste.
- Streaming providers and torrent resolution must run asynchronously with clean
  timeouts and failover mechanisms.

--------------------------------------------------------------------------------
6. TESTING & VERIFICATION
--------------------------------------------------------------------------------
- When modifying category menus, discover pages, or catalog logic, always run:
    python3 -m unittest test_category_menus.py
    python3 -m unittest test_continue_watching.py test_collections.py
- Benchmark data preparation and model construction times when touching menu
  logic to guarantee operations complete in <5ms.
================================================================================
