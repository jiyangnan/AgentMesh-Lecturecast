(() => {
  const coreOrigin = "https://agentmesh360.com";
  const launchLifetimeMs = 15 * 60 * 1000;
  const links = [...document.querySelectorAll("[data-checkin-link]")];
  let activeLaunch = null;

  const requestId = () =>
    globalThis.crypto?.randomUUID?.().replaceAll("-", "") ||
    `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

  for (const link of links) {
    link.addEventListener("click", (event) => {
      if (
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      event.preventDefault();
      const id = requestId();
      const target = new URL(link.href);
      target.searchParams.set("source_origin", window.location.origin);
      target.searchParams.set("source_surface", link.dataset.checkinSurface);
      target.searchParams.set("request_id", id);
      const popup = window.open(
        target.toString(),
        "agentmesh360-checkin",
        "popup=yes,width=520,height=760",
      );
      if (!popup) {
        window.location.assign(target.toString());
        return;
      }
      activeLaunch = { id, link, popup, startedAt: Date.now() };
    });
  }

  window.addEventListener("message", (event) => {
    if (!activeLaunch || Date.now() - activeLaunch.startedAt > launchLifetimeMs) {
      activeLaunch = null;
      return;
    }
    if (
      event.origin !== coreOrigin ||
      event.source !== activeLaunch.popup ||
      event.data?.type !== "agentmesh360:checkin-complete" ||
      event.data?.request_id !== activeLaunch.id ||
      event.data?.state !== "claimed"
    ) {
      return;
    }
    const current = Number(event.data.progress_current || 0);
    const target = Number(event.data.progress_target || 7);
    const label = activeLaunch.link.dataset.checkinCompleteLabel || "Checked in";
    activeLaunch.link.textContent = current ? `${label} ${current}/${target}` : label;
    activeLaunch.link.dataset.checkinState = "claimed";
    activeLaunch = null;
  });
})();
