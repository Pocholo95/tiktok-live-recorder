/*
 * Library video player: custom controls on top of a plain <video>.
 *
 * - Seek bar with buffered ranges, preview thumbnails (storyboard sheets
 *   from /media/<id>/storyboard), saved clips, the current clip selection
 *   and a lane with who was on screen. Dragging only seeks on release, so
 *   scrubbing a 3 h video doesn't fire dozens of slow seeks.
 * - Fine seeking: -10/-1/+1/+10 s buttons, frame steps, speed, keyboard.
 * - Self-recovery: if playback is stuck loading, reload the source at the
 *   same position instead of needing F5.
 *
 * initPlayer(root) is called by partials/library_player.html every time a
 * recording is shown; the previous instance is torn down first.
 */
(function () {
  const FRAME = 1 / 30; // converted MP4s are 30 fps
  const RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3];
  const STUCK_MS = 8000;
  const PREFS_KEY = "player-prefs";

  let current = null; // teardown of the active instance

  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch (e) { return {}; }
  }
  function savePrefs(patch) {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify({ ...loadPrefs(), ...patch })); } catch (e) { /* ignore */ }
  }

  function fmt(t) {
    t = Math.max(0, Math.floor(t || 0));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  // Same formula as web.formatting.avatar_hue, so colors match the rest.
  function hue(name) {
    let total = 0;
    for (let i = 0; i < name.length; i++) total += name.charCodeAt(i) * (i + 1);
    return total % 360;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  const ICONS = {
    play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
    vol: '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg>',
    mute: '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4zm15.6 3 2.1-2.1-1.4-1.4-2.1 2.1-2.1-2.1-1.4 1.4 2.1 2.1-2.1 2.1 1.4 1.4 2.1-2.1 2.1 2.1 1.4-1.4z"/></svg>',
    full: '<svg viewBox="0 0 24 24"><path d="M4 4h6v2H6v4H4zm10 0h6v6h-2V6h-4zM4 14h2v4h4v2H4zm14 4v-4h2v6h-6v-2z"/></svg>',
  };

  function button(cls, html, title) {
    const b = el("button", "pc-btn " + cls);
    b.type = "button";
    b.innerHTML = html;
    b.title = title;
    b.setAttribute("aria-label", title);
    return b;
  }

  function initPlayer(root) {
    if (current) current();
    const video = root.querySelector("video");
    const recordingId = root.dataset.recordingId;
    let guests = [];
    try { guests = JSON.parse(root.dataset.guests || "[]"); } catch (e) { guests = []; }
    const prefs = loadPrefs();
    const cleanups = [];
    const on = (target, type, fn, opts) => {
      target.addEventListener(type, fn, opts);
      cleanups.push(() => target.removeEventListener(type, fn, opts));
    };

    video.controls = false;
    video.playbackRate = prefs.rate || 1;
    video.volume = prefs.volume == null ? 1 : prefs.volume;
    video.muted = !!prefs.muted;

    // --- DOM ------------------------------------------------------------
    const overlay = el("div", "pc-overlay");
    const spinner = el("div", "pc-spinner");
    const notice = el("div", "pc-notice");
    overlay.append(spinner, notice);
    video.insertAdjacentElement("afterend", overlay);

    const controls = el("div", "pc");
    controls.id = "player-controls";

    const bar = el("div", "pc-bar");
    bar.tabIndex = -1;
    const lane = el("div", "pc-lane");
    const track = el("div", "pc-track");
    const buffered = el("div", "pc-buffered");
    const clips = el("div", "pc-clips");
    const selection = el("div", "pc-selection");
    const progress = el("div", "pc-progress");
    const hoverLine = el("div", "pc-hover-line");
    const knob = el("div", "pc-knob");
    track.append(buffered, clips, selection, progress, hoverLine);
    const preview = el("div", "pc-preview");
    const thumb = el("div", "pc-thumb");
    const previewTime = el("div", "pc-preview-time");
    const previewWho = el("div", "pc-preview-who");
    preview.append(thumb, previewTime, previewWho);
    bar.append(lane, track, knob, preview);

    const row = el("div", "pc-row");
    const back10 = button("pc-step", "−10", "Atrás 10 s (J)");
    const back1 = button("pc-step", "−1", "Atrás 1 s (Shift+←)");
    const play = button("pc-play", ICONS.play, "Reproducir (espacio)");
    const fwd1 = button("pc-step", "+1", "Adelante 1 s (Shift+→)");
    const fwd10 = button("pc-step", "+10", "Adelante 10 s (L)");
    const time = el("span", "pc-time mono", "0:00 / 0:00");
    const spacer = el("span", "pc-spacer");
    const framePrev = button("pc-step pc-frame", "◂|", "Cuadro anterior (,)");
    const frameNext = button("pc-step pc-frame", "|▸", "Cuadro siguiente (.)");
    const rate = el("select", "pc-rate");
    rate.title = "Velocidad";
    rate.setAttribute("aria-label", "Velocidad");
    RATES.forEach((r) => {
      const o = el("option", null, r + "×");
      o.value = r;
      rate.append(o);
    });
    rate.value = String(video.playbackRate);
    const mute = button("pc-mute", ICONS.vol, "Silenciar (M)");
    const volume = el("input", "pc-volume");
    volume.type = "range";
    volume.min = 0; volume.max = 1; volume.step = 0.05;
    volume.setAttribute("aria-label", "Volumen");
    const full = button("pc-full", ICONS.full, "Pantalla completa (F)");
    row.append(back10, back1, play, fwd1, fwd10, time, spacer, framePrev, frameNext, rate, mute, volume, full);
    controls.append(bar, row);
    root.append(controls);
    cleanups.push(() => { overlay.remove(); controls.remove(); });

    // --- Playback -------------------------------------------------------
    const duration = () => (Number.isFinite(video.duration) ? video.duration : 0);
    function seekTo(t) {
      const d = duration();
      video.currentTime = Math.min(Math.max(0, t), d ? d - 0.05 : t);
      update();
    }
    function seekBy(dt) { seekTo((pendingSeek ?? video.currentTime) + dt); }
    function toggle() { if (video.paused) video.play().catch(() => {}); else video.pause(); }
    function step(frames) { video.pause(); seekTo(video.currentTime + frames * FRAME); }
    function fullscreen() {
      if (document.fullscreenElement) document.exitFullscreen();
      else root.requestFullscreen && root.requestFullscreen().catch(() => {});
    }

    on(play, "click", toggle);
    on(video, "click", toggle);
    on(video, "dblclick", fullscreen);
    on(back10, "click", () => seekBy(-10));
    on(back1, "click", () => seekBy(-1));
    on(fwd1, "click", () => seekBy(1));
    on(fwd10, "click", () => seekBy(10));
    on(framePrev, "click", () => step(-1));
    on(frameNext, "click", () => step(1));
    on(full, "click", fullscreen);
    on(rate, "change", () => {
      video.playbackRate = parseFloat(rate.value);
      savePrefs({ rate: video.playbackRate });
    });
    on(mute, "click", () => { video.muted = !video.muted; });
    on(volume, "input", () => {
      video.volume = parseFloat(volume.value);
      video.muted = video.volume === 0;
    });
    on(video, "volumechange", () => {
      mute.innerHTML = video.muted || video.volume === 0 ? ICONS.mute : ICONS.vol;
      volume.value = video.muted ? 0 : video.volume;
      savePrefs({ volume: video.volume, muted: video.muted });
    });
    video.dispatchEvent(new Event("volumechange"));

    // --- Seek bar -------------------------------------------------------
    let pendingSeek = null; // position being dragged to (not sought yet)
    const pct = (t) => (duration() ? (t / duration()) * 100 : 0) + "%";

    function update() {
      const t = pendingSeek ?? video.currentTime;
      progress.style.width = pct(t);
      knob.style.left = pct(t);
      time.textContent = `${fmt(t)} / ${fmt(duration())}`;
      play.innerHTML = video.paused ? ICONS.play : ICONS.pause;
      play.title = video.paused ? "Reproducir (espacio)" : "Pausar (espacio)";
      let end = 0;
      for (let i = 0; i < video.buffered.length; i++) {
        if (video.buffered.start(i) <= video.currentTime + 0.5) end = Math.max(end, video.buffered.end(i));
      }
      buffered.style.left = pct(video.currentTime);
      buffered.style.width = pct(Math.max(0, end - video.currentTime));
    }
    ["timeupdate", "play", "pause", "progress", "durationchange", "seeked", "loadedmetadata"].forEach((ev) => on(video, ev, update));

    function timeAt(clientX) {
      const r = track.getBoundingClientRect();
      return Math.min(Math.max(0, (clientX - r.left) / r.width), 1) * duration();
    }

    let storyboard = null;
    function showPreview(clientX) {
      const d = duration();
      if (!d) return;
      const t = timeAt(clientX);
      const r = bar.getBoundingClientRect();
      const x = clientX - r.left;
      hoverLine.style.left = pct(t);
      previewTime.textContent = fmt(t);
      const who = guests
        .filter((g) => t >= g.start_seconds && t < (g.end_seconds == null ? Infinity : g.end_seconds))
        .map((g) => "@" + g.username);
      previewWho.textContent = [...new Set(who)].join(" · ");
      previewWho.hidden = !who.length;
      if (storyboard) {
        const sb = storyboard;
        const index = Math.min(Math.floor(t / sb.interval), sb.count - 1);
        const perSheet = sb.cols * sb.rows;
        const sheet = Math.floor(index / perSheet) + 1;
        const cell = index % perSheet;
        thumb.style.width = sb.tile_width + "px";
        thumb.style.height = sb.tile_height + "px";
        thumb.style.backgroundImage = `url(/media/${recordingId}/storyboard/${sheet}.jpg)`;
        thumb.style.backgroundPosition =
          `-${(cell % sb.cols) * sb.tile_width}px -${Math.floor(cell / sb.cols) * sb.tile_height}px`;
        thumb.hidden = false;
      } else {
        thumb.hidden = true;
      }
      preview.classList.add("is-on");
      bar.classList.add("is-hover");
      const half = preview.offsetWidth / 2;
      preview.style.left = Math.min(Math.max(x, half), r.width - half) + "px";
    }
    function hidePreview() {
      if (pendingSeek != null) return;
      preview.classList.remove("is-on");
      bar.classList.remove("is-hover");
    }

    on(bar, "pointermove", (e) => {
      showPreview(e.clientX);
      if (pendingSeek != null) { pendingSeek = timeAt(e.clientX); update(); }
    });
    on(bar, "pointerleave", hidePreview);
    on(bar, "pointerdown", (e) => {
      if (!duration()) return;
      try { bar.setPointerCapture(e.pointerId); } catch (err) { /* synthetic event */ }
      pendingSeek = timeAt(e.clientX);
      bar.classList.add("is-dragging");
      showPreview(e.clientX);
      update();
    });
    const release = (e) => {
      if (pendingSeek == null) return;
      const t = pendingSeek;
      pendingSeek = null;
      bar.classList.remove("is-dragging");
      seekTo(t);
      if (e.type === "pointercancel" || !bar.matches(":hover")) hidePreview();
    };
    on(bar, "pointerup", release);
    on(bar, "pointercancel", release);

    // Saved clips + the clip being cut, drawn on the bar.
    function drawClips() {
      const d = duration();
      clips.replaceChildren();
      if (!d) return;
      document.querySelectorAll("#clip-marks-list .mark[data-start]").forEach((m) => {
        const s = parseFloat(m.dataset.start), e = parseFloat(m.dataset.end);
        const band = el("div", "pc-clip");
        band.style.left = pct(s);
        band.style.width = pct(Math.max(e - s, d / 400));
        band.title = (m.dataset.label || "Clip") + ` · ${fmt(s)} → ${fmt(e)}`;
        clips.append(band);
      });
    }
    window.playerSetSelection = (s, e) => {
      if (!duration() || !(e > s)) { selection.hidden = true; return; }
      selection.hidden = false;
      selection.style.left = pct(s);
      selection.style.width = pct(e - s);
    };
    cleanups.push(() => { delete window.playerSetSelection; });
    selection.hidden = true;

    function drawLane() {
      const d = duration();
      lane.replaceChildren();
      if (!d || !guests.length) { lane.hidden = true; return; }
      lane.hidden = false;
      guests.forEach((g) => {
        const end = g.end_seconds == null ? d : Math.min(g.end_seconds, d);
        const seg = el("div", "pc-guest");
        seg.style.left = pct(g.start_seconds);
        seg.style.width = pct(Math.max(end - g.start_seconds, d / 500));
        seg.style.setProperty("--hue", hue(g.username));
        lane.append(seg);
      });
    }
    on(video, "loadedmetadata", () => { drawClips(); drawLane(); });
    on(document, "htmx:afterSettle", () => {
      if (!root.isConnected) { if (current) current(); return; }
      drawClips();
    });
    if (video.readyState >= 1) { drawClips(); drawLane(); }

    fetch(`/media/${recordingId}/storyboard`)
      .then((r) => (r.ok ? r.json() : null))
      .then((sb) => {
        if (!sb) return;
        storyboard = sb;
        // Warm the first sheet so the first hover isn't blank.
        new Image().src = `/media/${recordingId}/storyboard/1.jpg`;
      })
      .catch(() => {});

    // --- Buffering indicator + self-recovery ---------------------------
    // Stuck loading for STUCK_MS while playing, or the source failed:
    // reload it and continue where it was (and playing if it was). Retries
    // back off (2, 4, 8... s); after MAX_RETRIES the notice offers a retry.
    const MAX_RETRIES = 6;
    let stuckTimer = null;
    let retryTimer = null;
    let resume = null; // {at, play} while recovering
    let attempts = 0;

    function setNotice(text, retry) {
      notice.textContent = text || "";
      notice.hidden = !text;
      notice.classList.toggle("is-action", !!retry);
    }
    setNotice("");
    on(notice, "click", () => {
      if (!notice.classList.contains("is-action")) return;
      attempts = 0;
      recover();
    });

    function waiting() {
      spinner.hidden = false;
      clearTimeout(stuckTimer);
      if (!video.paused) stuckTimer = setTimeout(recover, STUCK_MS);
    }
    function flowing() {
      spinner.hidden = true;
      clearTimeout(stuckTimer);
      if (!resume) setNotice("");
    }
    function recover() {
      clearTimeout(stuckTimer);
      clearTimeout(retryTimer);
      if (!resume) resume = { at: video.currentTime, play: !video.paused };
      if (attempts >= MAX_RETRIES) {
        spinner.hidden = true;
        setNotice("No se pudo cargar el video. Tocá para reintentar.", true);
        return;
      }
      attempts += 1;
      spinner.hidden = false;
      setNotice("Reconectando…");
      video.load();
    }
    function retryLater() {
      if (!resume && video.paused && !video.currentSrc) return;
      if (!resume) resume = { at: video.currentTime, play: !video.paused };
      clearTimeout(retryTimer);
      retryTimer = setTimeout(recover, Math.min(2000 * 2 ** attempts, 30000));
      setNotice("Sin conexión con el hub, reintentando…");
    }
    on(video, "loadedmetadata", () => {
      if (!resume) return;
      video.currentTime = resume.at;
      if (resume.play) video.play().catch(() => {});
    });
    on(video, "playing", () => { attempts = 0; resume = null; flowing(); });
    on(video, "seeked", () => {
      if (resume && !resume.play) { attempts = 0; resume = null; }
      if (video.readyState >= 3) flowing();
    });
    spinner.hidden = true;
    on(video, "waiting", waiting);
    on(video, "seeking", () => { spinner.hidden = false; });
    on(video, "stalled", () => { if (!video.paused && video.readyState < 3) waiting(); });
    on(video, "canplay", () => { if (!resume || !resume.play) flowing(); });
    on(video, "pause", () => { if (!resume) flowing(); });
    // With <source> children a failed load fires "error" on the <source>,
    // which only reaches the video in the capture phase.
    on(video, "error", retryLater, true);
    cleanups.push(() => { clearTimeout(stuckTimer); clearTimeout(retryTimer); });

    // --- Keyboard -------------------------------------------------------
    on(document, "keydown", (e) => {
      if (!root.isConnected) { if (current) current(); return; }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const tag = (e.target.tagName || "").toLowerCase();
      if (["input", "textarea", "select"].includes(tag) || e.target.isContentEditable) return;
      if (e.target.closest && e.target.closest("button") && (e.key === " " || e.key === "Enter")) return;
      const k = e.key;
      const handled = {
        " ": toggle, k: toggle, K: toggle,
        j: () => seekBy(-10), J: () => seekBy(-10),
        l: () => seekBy(10), L: () => seekBy(10),
        ArrowLeft: () => seekBy(e.shiftKey ? -1 : -5),
        ArrowRight: () => seekBy(e.shiftKey ? 1 : 5),
        ",": () => step(-1), ".": () => step(1),
        m: () => { video.muted = !video.muted; }, M: () => { video.muted = !video.muted; },
        f: fullscreen, F: fullscreen,
      }[k];
      if (handled) { e.preventDefault(); handled(); return; }
      if (/^[0-9]$/.test(k) && duration()) { e.preventDefault(); seekTo((duration() * Number(k)) / 10); }
    });

    update();
    current = () => {
      cleanups.forEach((fn) => fn());
      current = null;
    };
  }

  window.initPlayer = initPlayer;
})();
