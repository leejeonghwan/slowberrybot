const trendList = document.getElementById("trendList");
const updatedAtLabel = document.getElementById("updatedAtLabel");
const retrievedAtLabel = document.getElementById("retrievedAtLabel");
const trendSummary = document.getElementById("trendSummary");
const noticeText = document.getElementById("noticeText");
const statusText = document.getElementById("statusText");
const statusPill = document.getElementById("statusPill");
const refreshButton = document.getElementById("refreshButton");
const sectionNote = document.getElementById("sectionNote");

const appConfig = window.DAUM_TRENDS_APP_CONFIG || {
  dataUrl: "./api/trends",
  refreshLabel: "지금 새로고침",
  autoRefreshMs: 60_000,
  sectionNote: "1분마다 자동 새로고침됩니다.",
  statusMessage: "실시간 트렌드를 받아오는 중입니다.",
  staleMessage: "마지막으로 배포된 데이터를 표시 중입니다.",
};

const formatter = new Intl.DateTimeFormat("ko-KR", {
  dateStyle: "medium",
  timeStyle: "short",
});

let refreshTimer = null;

refreshButton.textContent = appConfig.refreshLabel;
sectionNote.textContent = appConfig.sectionNote;
statusText.textContent = appConfig.statusMessage;

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function statusClass(status) {
  if (status === "상승" || status === "하락" || status === "신규") {
    return status;
  }
  if (status === "동일") {
    return "동일";
  }
  return "변동없음";
}

function setStatus(kind, message) {
  statusPill.className = `status-pill ${kind}`;
  statusPill.textContent =
    kind === "ok" ? "정상" : kind === "error" ? "주의" : "로딩 중";
  statusText.textContent = message;
}

function formatRetrievedAt(value) {
  try {
    return formatter.format(new Date(value));
  } catch (error) {
    return value;
  }
}

function buildSummary(items) {
  const counts = items.reduce(
    (acc, item) => {
      const key = item.status || "변동없음";
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    },
    {}
  );

  const order = ["상승", "하락", "신규", "동일", "변동없음"];
  const parts = order
    .filter((key) => counts[key])
    .map((key) => `${key} ${counts[key]}건`);

  return parts.length ? parts.join(" / ") : "표시할 항목이 없습니다.";
}

function renderTrends(items) {
  if (!items.length) {
    trendList.innerHTML = `<li class="empty-state">표시할 트렌드가 없습니다.</li>`;
    return;
  }

  trendList.innerHTML = items
    .map(
      (item, index) => `
        <li class="trend-item" style="animation-delay:${index * 50}ms">
          <a class="trend-card" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">
            <span class="rank-badge">${escapeHtml(item.rank)}</span>
            <div class="trend-copy">
              <p class="trend-keyword">${escapeHtml(item.keyword)}</p>
              <p class="trend-sub">Daum 검색 결과로 이동</p>
            </div>
            <span class="status-chip ${statusClass(item.status)}">${escapeHtml(item.status)}</span>
          </a>
        </li>
      `
    )
    .join("");
}

async function loadTrends(force = false) {
  setStatus("loading", "Daum 실시간 트렌드를 업데이트하고 있습니다.");
  refreshButton.disabled = true;

  try {
    const separator = appConfig.dataUrl.includes("?") ? "&" : "?";
    const forceQuery = force && appConfig.dataUrl.includes("/api/")
      ? `${separator}force=1`
      : `${separator}ts=${Date.now()}`;
    const response = await fetch(`${appConfig.dataUrl}${force ? forceQuery : ""}`, {
      cache: "no-store",
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.details || payload.error || "알 수 없는 오류");
    }

    updatedAtLabel.textContent = payload.updated_at_label;
    retrievedAtLabel.textContent = formatRetrievedAt(payload.retrieved_at);
    trendSummary.textContent = buildSummary(payload.items);
    noticeText.textContent =
      payload.notice || "서비스 메모가 제공되지 않았습니다.";
    renderTrends(payload.items);
    if (payload.stale) {
      setStatus(
        "error",
        payload.warning
          ? `${appConfig.staleMessage} (${payload.warning})`
          : appConfig.staleMessage
      );
    } else {
      setStatus("ok", `${payload.item_count}개의 키워드를 반영했습니다.`);
    }
  } catch (error) {
    setStatus("error", error.message);
    trendList.innerHTML = `
      <li class="empty-state">
        실시간 트렌드를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.
      </li>
    `;
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", () => loadTrends(true));

loadTrends();
if (appConfig.autoRefreshMs > 0) {
  refreshTimer = window.setInterval(() => loadTrends(false), appConfig.autoRefreshMs);
}

window.addEventListener("beforeunload", () => {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
  }
});
