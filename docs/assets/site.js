(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const systemTheme = matchMedia("(prefers-color-scheme: dark)");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const isDark = () =>
    document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : systemTheme.matches;
  function syncThemeButton() {
    // Native picture selection follows the OS unless the visitor chose a theme.
    const theme = document.documentElement.dataset.theme;
    document
      .querySelectorAll("picture[data-theme-picture] source[data-dark-source]")
      .forEach((source) => {
        const scope = source.hasAttribute("data-mobile-source")
          ? "(max-width: 600px)"
          : "all";
        source.media =
          theme === "dark"
            ? scope
            : theme === "light"
              ? "not all"
              : scope === "all"
                ? "(prefers-color-scheme: dark)"
                : `${scope} and (prefers-color-scheme: dark)`;
      });
    $("theme-toggle").title = isDark()
      ? "Switch to light mode"
      : "Switch to dark mode";
    $("theme-toggle").setAttribute(
      "aria-label",
      `Switch to ${isDark() ? "light" : "dark"} mode`,
    );
  }
  $("theme-toggle").addEventListener("click", () => {
    const theme = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("aether-theme", theme);
    } catch (e) {}
    syncThemeButton();
  });
  systemTheme.addEventListener("change", syncThemeButton);
  syncThemeButton();

  // Native document navigation, with a short arrival transition on pointer clicks only.
  // Nothing waits for animation; links, history, and no-JS navigation remain native.
  const motions = new Map();
  const ease = getComputedStyle(document.documentElement)
    .getPropertyValue("--ease")
    .trim();
  function reveal(element, duration = 240, distance = 6) {
    if (reducedMotion.matches || !element.animate) return;
    motions.get(element)?.cancel();
    const motion = element.animate(
      [
        { opacity: 0.65, transform: `translateY(${distance}px)` },
        { opacity: 1, transform: "translateY(0)" },
      ],
      { duration, easing: ease || "ease-out" },
    );
    motions.set(element, motion);
    motion.onfinish = () => {
      if (motions.get(element) === motion) motions.delete(element);
    };
  }
  try {
    const arrival = JSON.parse(
      sessionStorage.getItem("aether-arrival") || "null",
    );
    sessionStorage.removeItem("aether-arrival");
    if (
      arrival &&
      arrival.url === location.href &&
      Date.now() - arrival.at < 10000
    )
      reveal($("main"));
  } catch (e) {}
  document.addEventListener("keydown", () => {
    document.documentElement.dataset.input = "keyboard";
  });
  document.addEventListener("pointerdown", () => {
    document.documentElement.dataset.input = "pointer";
  });
  document.addEventListener("click", (event) => {
    if (
      event.defaultPrevented ||
      !event.detail ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    const link = event.target.closest("a[href]");
    if (!link || link.target || link.hasAttribute("download")) return;
    const destination = new URL(link.href, location.href);
    if (
      destination.origin !== location.origin ||
      destination.pathname === location.pathname
    )
      return;
    try {
      sessionStorage.setItem(
        "aether-arrival",
        JSON.stringify({ url: destination.href, at: Date.now() }),
      );
    } catch (e) {}
  });
  reducedMotion.addEventListener("change", () => {
    if (reducedMotion.matches) {
      for (const motion of motions.values()) motion.cancel();
      motions.clear();
    }
  });

  if ($("demo-command")) {
    // This is a scripted simulation. No commands are executed and no data leaves the page.
    let step = reducedMotion.matches ? 3 : 0;
    let timer = null;
    let playing = !reducedMotion.matches;
    const labels = ["Remember", "Change the file", "Recall", "Replace"];
    const announcements = [
      "Step 1 of 4. Claude Code remembers that the database runs Postgres via docker compose. Saved as m1, linked to docker-compose.yml.",
      "Step 2 of 4. A teammate changes docker-compose.yml: image postgres:16 is replaced with mysql:8. The memory is waiting for a file check.",
      "Step 3 of 4. A new Cursor session recalls database setup. Memory m1 is now marked outdated: docker-compose.yml changed.",
      "Step 4 of 4. The old memory is replaced by m2: The database runs MySQL 8. History retains the original memory, the reason it became stale, and its replacement.",
    ];
    function stop() {
      clearTimeout(timer);
      timer = null;
      playing = false;
      $("pause").textContent = "Play";
    }
    function render(announce = false, animate = false) {
      $("session-label").textContent =
        step < 2 ? "Claude Code · session 01" : "Cursor · session 02";
      $("terminal-label").textContent = [
        "The first session",
        "A teammate edits the file",
        "A new session checks the facts",
        "The current fact replaces the old one",
      ][step];
      $("demo-command").textContent =
        step < 2
          ? '> remember("The database runs Postgres via docker compose", files=["docker-compose.yml"])'
          : step === 2
            ? '> recall("database setup")'
            : "> The database runs MySQL 8. Replace the old Postgres memory.";
      $("demo-output").textContent = [
        "Saved as m1.",
        "File edited. The old fact needs checking.",
        "m1 is now marked outdated: docker-compose.yml changed.",
        "m1 replaced by m2. History preserved.",
      ][step];
      $("demo-output").style.color =
        step === 2
          ? "var(--stale)"
          : step === 1
            ? "var(--muted)"
            : "var(--active)";
      $("file-state").textContent =
        step === 0 ? "saved version" : "working tree diff";
      $("diff-code").innerHTML =
        '<span class="diff-line">  services:</span><span class="diff-line">    db:</span>' +
        (step === 0
          ? '<span class="diff-line">      image: postgres:16</span>'
          : '<span class="diff-line removed">−     image: postgres:16</span><span class="diff-line added">+     image: mysql:8</span>');
      $("memory-id").textContent = step === 3 ? "m2 · replaces m1" : "m1";
      $("memory-status").textContent = [
        "Active",
        "Pending check",
        "Outdated",
        "Active",
      ][step];
      $("memory-status").className =
        "badge" + (step === 2 ? " outdated" : step === 1 ? " pending" : "");
      $("memory-copy").textContent =
        step === 3
          ? "The database runs MySQL 8"
          : "The database runs Postgres via docker compose";
      $("memory-copy").className =
        "memory-copy" + (step === 2 ? " retired" : "");
      $("memory-meta").innerHTML =
        "Linked to <code>docker-compose.yml</code> · " +
        (step === 3 ? "updated file version" : "git content hash");
      $("history").innerHTML =
        "<li" +
        (step < 2 ? ' class="current"' : "") +
        ">Created<span>Claude Code · session 01</span></li>" +
        (step >= 2
          ? '<li class="stale">Went stale<span>docker-compose.yml changed</span></li>'
          : "") +
        (step === 3
          ? '<li class="current">Replaced by m2<span>The database runs MySQL 8</span></li>'
          : "");
      $("step-indicator").innerHTML =
        labels
          .map((_, i) => `<i class="${i <= step ? "filled" : ""}"></i>`)
          .join("") + `<span>${step + 1} of 4 · ${labels[step]}</span>`;
      $("next").textContent = [
        "Edit the file",
        "Next: recall",
        "Next: replace",
        "Complete",
      ][step];
      $("next").disabled = step === 3;
      $("pause").hidden = step === 3;
      $("pause").textContent = playing ? "Pause" : "Play";
      if (animate) {
        for (const id of [
          "demo-output",
          "diff-code",
          "memory-copy",
          "memory-status",
          "history",
        ]) {
          reveal($(id), 220, 3);
        }
      }
      if (announce) $("demo-announcement").textContent = announcements[step];
    }
    function schedule() {
      clearTimeout(timer);
      if (!playing || step >= 3) return;
      timer = setTimeout(
        () => {
          step++;
          if (step === 3) playing = false;
          render(true, true);
          schedule();
        },
        step === 0 ? 5000 : 4000,
      );
    }
    $("next").addEventListener("click", (event) => {
      stop();
      if (step < 3) step++;
      render(true, event.detail > 0);
    });
    $("pause").addEventListener("click", () => {
      if (playing) stop();
      else {
        playing = true;
        schedule();
      }
      render();
    });
    $("replay").addEventListener("click", (event) => {
      stop();
      step = 0;
      playing = !reducedMotion.matches;
      render(true, event.detail > 0);
      schedule();
    });
    reducedMotion.addEventListener("change", () => {
      if (reducedMotion.matches) {
        stop();
        step = 3;
        render();
      }
    });
    // Do not keep advancing a simulation in a hidden tab.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && playing) {
        stop();
        render();
      }
    });
    render();
    schedule();
  }
  document.querySelectorAll("[data-copy]").forEach((button) =>
    button.addEventListener("click", async () => {
      try {
        if (navigator.clipboard && window.isSecureContext)
          await navigator.clipboard.writeText(button.dataset.copy);
        else {
          const input = document.createElement("textarea");
          input.value = button.dataset.copy;
          input.style.position = "fixed";
          input.style.opacity = "0";
          document.body.append(input);
          input.select();
          const copied = document.execCommand("copy");
          input.remove();
          button.focus();
          if (!copied) throw new Error("Copy unavailable");
        }
        button.textContent = "Copied";
        $("copy-status").textContent = "Command copied to clipboard.";
        setTimeout(() => {
          button.textContent = "Copy";
        }, 2000);
      } catch (e) {
        button.textContent = "Retry";
        $("copy-status").textContent =
          "Could not copy automatically. Select the command and copy it manually.";
      }
    }),
  );
})();
