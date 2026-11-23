/**
 * DocuVault AI — Async Drag-and-Drop Intake Engine (upload.js)
 * Uploads files, then polls /api/documents/<id>/status/ until Gemini + SharePoint finish.
 */

(function () {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const queue = document.getElementById("queue");

  if (!dropzone || !fileInput || !queue) return;
  if (dropzone.dataset.initialized === "true") return;
  dropzone.dataset.initialized = "true";

  fileInput.multiple = true;
  const MAX_BATCH = 10;

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("dropzone-active");
    })
  );

  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("dropzone-active");
    })
  );

  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    handleFiles(e.dataTransfer.files);
  });

  fileInput.addEventListener("change", (e) => {
    handleFiles(e.target.files);
    fileInput.value = "";
  });

  function handleFiles(fileList) {
    let files = Array.from(fileList);
    if (!files.length) return;

    if (files.length > MAX_BATCH) {
      dvToast(`Batch limit is ${MAX_BATCH} documents per upload. Ingesting first ${MAX_BATCH} scans.`, "warning");
      files = files.slice(0, MAX_BATCH);
    }

    const rows = files.map((file) => queueFile(file));
    processSequentially(files, rows);
  }

  function queueFile(file) {
    const row = document.createElement("div");
    row.className = "rise-in bg-white rounded-2xl p-4 border border-slate-200 shadow-sm transition-all duration-200";
    row.dataset.filename = file.name;
    row.innerHTML = `
      <div class="flex items-center justify-between gap-3">
        <div class="flex items-center space-x-3 min-w-0">
          <div class="w-9 h-9 rounded-xl bg-slate-100 border border-slate-200 flex items-center justify-center shrink-0 text-indigo-600">
            <i data-lucide="file-up" class="w-4 h-4"></i>
          </div>
          <div class="min-w-0">
            <p class="text-xs sm:text-sm font-semibold text-slate-800 truncate">${escapeHtml(file.name)}</p>
            <p class="text-[11px] text-slate-500 status-text mt-0.5">Queued for upload…</p>
          </div>
        </div>
        <span class="status-badge text-[11px] font-semibold shrink-0 rounded-full px-2.5 py-0.5 bg-slate-100 border border-slate-200 text-slate-600">
          Queued
        </span>
      </div>
      <div class="mt-3 h-1.5 bg-slate-100 rounded-full overflow-hidden border border-slate-200/70">
        <div class="progress-fill h-full bg-gradient-to-r from-indigo-600 to-sky-500 rounded-full" style="width: 8%"></div>
      </div>
    `;
    queue.prepend(row);
    if (window.lucide) lucide.createIcons({ root: row });
    return row;
  }

  async function processSequentially(files, rows) {
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const row = rows[i];

      setRowState(
        row,
        "Uploading scan to secure staging…",
        "Uploading",
        "bg-indigo-50 text-indigo-700 border border-indigo-200",
        30
      );

      const formData = new FormData();
      formData.append("temp_file", file);

      try {
        const { data, ok } = await dvFetch("/api/upload/", { method: "POST", body: formData });
        if (!ok || !data.ok) {
          setRowState(
            row,
            (data && data.error) || "Upload rejected.",
            "Rejected",
            "bg-rose-50 text-rose-700 border border-rose-200",
            100
          );
          continue;
        }

        row.dataset.docId = data.id;
        if (data.is_done) {
          applyResult(row, data);
        } else {
          setRowState(
            row,
            "AI reading content & categorizing…",
            "Processing",
            "bg-sky-50 text-sky-700 border border-sky-200 status-pulse",
            65
          );
          pollUntilDone(row, data.id);
        }
      } catch (err) {
        setRowState(
          row,
          "Network error during upload.",
          "Error",
          "bg-rose-50 text-rose-700 border border-rose-200",
          100
        );
      }
    }
  }

  async function pollUntilDone(row, docId) {
    const maxPolls = 60; // ~90 seconds
    for (let attempt = 0; attempt < maxPolls; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const { data, ok } = await dvFetch(`/api/documents/${docId}/status/`);
        if (!ok || !data.ok) continue;

        if (data.is_done) {
          applyResult(row, data);
          return;
        }

        const fill = row.querySelector(".progress-fill");
        const currentWidth = parseInt(fill.style.width, 10) || 65;
        if (currentWidth < 90) fill.style.width = `${currentWidth + 4}%`;
      } catch (err) {
        // Ignore transient poll errors
      }
    }

    setRowState(
      row,
      "Still processing in background — check All Documents shortly.",
      "Background",
      "bg-sky-50 text-sky-700 border border-sky-200",
      95
    );
  }

  function applyResult(row, doc) {
    const fill = row.querySelector(".progress-fill");
    fill.style.width = "100%";

    const map = {
      filed: ["Filed to SharePoint", "bg-emerald-50 text-emerald-700 border border-emerald-200", "bg-emerald-500"],
      needs_review: ["Needs Review", "bg-amber-50 text-amber-700 border border-amber-200", "bg-amber-500"],
      failed: ["Failed", "bg-rose-50 text-rose-700 border border-rose-200", "bg-rose-500"],
    };

    const [label, badgeClasses, barColor] = map[doc.status] || [
      "Processed",
      "bg-slate-100 text-slate-700 border border-slate-200",
      "bg-indigo-600",
    ];
    fill.className = `progress-fill h-full ${barColor} rounded-full`;

    const badge = row.querySelector(".status-badge");
    badge.textContent = label;
    badge.className = `status-badge text-[11px] font-semibold shrink-0 rounded-full px-2.5 py-0.5 ${badgeClasses}`;

    const details = [];
    if (doc.category) details.push(doc.category);
    if (doc.company_name && doc.company_name !== "—") details.push(doc.company_name);
    if (doc.document_year) details.push(doc.document_year);
    if (doc.confidence_percent !== null && doc.confidence_percent !== undefined) {
      details.push(`${doc.confidence_percent}% confidence`);
    }

    row.querySelector(".status-text").textContent = details.length
      ? details.join(" · ")
      : (doc.ai_summary || doc.error_message || "Ingestion complete.");

    const existingActions = row.querySelector(".row-actions");
    if (existingActions) existingActions.remove();

    const actions = document.createElement("div");
    actions.className = "row-actions flex flex-wrap items-center gap-3 mt-2.5";

    if (doc.status === "needs_review" || doc.status === "failed") {
      const reviewBtn = document.createElement("button");
      reviewBtn.type = "button";
      reviewBtn.className = "text-xs font-semibold text-amber-700 hover:text-amber-800 flex items-center space-x-1";
      reviewBtn.innerHTML = `<span>Review &amp; File</span><i data-lucide="arrow-right" class="w-3 h-3"></i>`;
      reviewBtn.onclick = () => window.openReviewModal(doc.id);
      actions.appendChild(reviewBtn);

      const discardBtn = document.createElement("button");
      discardBtn.type = "button";
      discardBtn.className = "text-xs font-semibold text-slate-500 hover:text-rose-600";
      discardBtn.textContent = "Discard";
      discardBtn.onclick = () => window.deleteDoc(doc.id);
      actions.appendChild(discardBtn);
    } else if (doc.status === "filed") {
      const detailBtn = document.createElement("button");
      detailBtn.type = "button";
      detailBtn.className = "text-xs font-semibold text-indigo-600 hover:text-indigo-700 flex items-center space-x-1";
      detailBtn.innerHTML = `<span>View Telemetry</span><i data-lucide="arrow-right" class="w-3 h-3"></i>`;
      detailBtn.onclick = () => window.openDocModal(doc.id);
      actions.appendChild(detailBtn);

      if (doc.sharepoint_web_url) {
        const spLink = document.createElement("a");
        spLink.href = doc.sharepoint_web_url;
        spLink.target = "_blank";
        spLink.rel = "noopener noreferrer";
        spLink.className = "text-xs font-semibold text-emerald-700 hover:text-emerald-800 flex items-center space-x-1";
        spLink.innerHTML = `<span>Open in SharePoint</span><i data-lucide="external-link" class="w-3 h-3"></i>`;
        actions.appendChild(spLink);
      }
    }

    row.querySelector(".status-text").after(actions);
    if (window.lucide) lucide.createIcons({ root: row });
  }

  function setRowState(row, text, badgeLabel, badgeClasses, width) {
    row.querySelector(".status-text").textContent = text;
    row.querySelector(".progress-fill").style.width = `${width}%`;
    const badge = row.querySelector(".status-badge");
    badge.textContent = badgeLabel;
    badge.className = `status-badge text-[11px] font-semibold shrink-0 rounded-full px-2.5 py-0.5 ${badgeClasses}`;
  }
})();