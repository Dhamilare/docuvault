/**
 * DocuVault AI — Document Filtering, Pagination & Bulk Review Queue (documents.js)
 * Relies on global helpers (dvFetch, dvToast, openDocModal, openReviewModal) from app.js.
 */

window.dvRefreshList = window.dvRefreshList || async function (page) {
  const container =
    document.getElementById("table-container") ||
    document.getElementById("document-table-container");
  if (!container) return;

  const filterForm = document.getElementById("document-filter-form");
  const params = filterForm
    ? new URLSearchParams(new FormData(filterForm))
    : new URLSearchParams(window.location.search);

  if (page) params.set("page", page);

  const qs = params.toString();
  const url = `${window.location.pathname}${qs ? "?" + qs : ""}`;

  try {
    const res = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
    if (!res.ok) return;

    const overlay = document.getElementById("table-loading-overlay");
    const overlayHtml = overlay ? overlay.outerHTML : "";
    container.innerHTML = overlayHtml + (await res.text());

    window.history.replaceState({}, "", url);
    if (window.lucide) lucide.createIcons({ root: container });
  } catch (err) {
    console.error("dvRefreshList failed:", err);
  }
};

window.dvGoToPage = window.dvGoToPage || function (page) {
  if (
    document.getElementById("table-container") ||
    document.getElementById("document-table-container")
  ) {
    window.dvRefreshList(page);
  } else {
    const url = new URL(window.location.href);
    url.searchParams.set("page", page);
    window.location.href = url.toString();
  }
};

window.initBulkReviewQueue = function () {
  const selectAll = document.getElementById("select-all-queue");
  const bulkBar = document.getElementById("bulk-actions-bar");
  const selectedCountEl = document.getElementById("bulk-selected-count");

  if (!bulkBar) return;

  function getLiveCheckboxes() {
    return Array.from(document.querySelectorAll(".doc-checkbox"));
  }

  function updateBulkState() {
    const boxes = getLiveCheckboxes();
    const selected = boxes.filter((c) => c.checked);
    const count = selected.length;

    if (count > 0) {
      bulkBar.classList.remove("hidden");
      bulkBar.classList.add("flex");
      if (selectedCountEl) selectedCountEl.textContent = `${count} selected`;
    } else {
      bulkBar.classList.add("hidden");
      bulkBar.classList.remove("flex");
    }

    if (selectAll) {
      selectAll.checked = boxes.length > 0 && count === boxes.length;
    }
  }

  if (selectAll) {
    selectAll.addEventListener("change", (e) => {
      getLiveCheckboxes().forEach((c) => {
        c.checked = e.target.checked;
      });
      updateBulkState();
    });
  }

  document.addEventListener("change", (e) => {
    if (e.target && e.target.classList.contains("doc-checkbox")) {
      updateBulkState();
    }
  });

  updateBulkState();
};

window.bulkFileSelected = async function () {
  const selectedBoxes = document.querySelectorAll(".doc-checkbox:checked");
  const docIds = Array.from(selectedBoxes)
    .map((c) => parseInt(c.value, 10))
    .filter(Boolean);

  if (!docIds.length) return;
  if (!confirm(`Approve and file ${docIds.length} document(s) directly to SharePoint?`)) return;

  const btn = document.getElementById("btn-bulk-file");
  const origHtml = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="animate-pulse">Filing ${docIds.length} items…</span>`;
  }

  try {
    const { data, ok } = await window.dvFetch("/api/documents/bulk-file/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: docIds }),
    });

    if (ok && data.ok) {
      if (data.filed_count > 0) {
        window.dvToast(`Filed ${data.filed_count} document(s) to SharePoint.`, "success");
      }
      if (data.failed_count > 0) {
        const firstErr = Array.isArray(data.errors) && data.errors.length ? ` (${data.errors[0]})` : "";
        window.dvToast(`${data.failed_count} document(s) failed to file${firstErr}.`, "warning");
      }
      setTimeout(() => window.location.reload(), data.failed_count > 0 ? 1500 : 700);
    } else {
      window.dvToast((data && data.error) || "Bulk filing experienced errors.", "error");
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    }
  } catch (err) {
    window.dvToast("Network error during bulk filing.", "error");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origHtml;
    }
  }
};

window.bulkDiscardSelected = async function () {
  const selectedBoxes = document.querySelectorAll(".doc-checkbox:checked");
  const docIds = Array.from(selectedBoxes)
    .map((c) => parseInt(c.value, 10))
    .filter(Boolean);

  if (!docIds.length) return;
  if (!confirm(`Discard ${docIds.length} document(s)? Transient scans will be permanently deleted.`)) return;

  const btn = document.getElementById("btn-bulk-discard");
  const origHtml = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="animate-pulse">Discarding…</span>`;
  }

  try {
    const { data, ok } = await window.dvFetch("/api/documents/bulk-discard/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: docIds }),
    });

    if (ok && data.ok) {
      window.dvToast(`Discarded ${data.discarded_count} document(s).`, "info");
      setTimeout(() => window.location.reload(), 650);
    } else {
      window.dvToast((data && data.error) || "Bulk discard failed.", "error");
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    }
  } catch (err) {
    window.dvToast("Network error during bulk discard.", "error");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origHtml;
    }
  }
};