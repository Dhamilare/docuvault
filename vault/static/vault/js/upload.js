/**
 * DocuVault AI — Drag-and-Drop Intake Engine
 * Sequentially uploads scanned files to avoid API rate limits and gives instant feedback.
 */

(function () {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const queue = document.getElementById("queue");

  if (!dropzone || !fileInput || !queue) return;

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("border-indigo-500", "bg-indigo-950/20");
    })
  );

  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("border-indigo-500", "bg-indigo-950/20");
    })
  );

  dropzone.addEventListener("drop", (e) => handleFiles(e.dataTransfer.files));
  fileInput.addEventListener("change", (e) => handleFiles(e.target.files));

  function handleFiles(fileList) {
    const files = Array.from(fileList);
    if (!files.length) return;
    files.forEach(queueFile);
    processSequentially(files);
  }

  function queueFile(file) {
    const row = document.createElement("div");
    row.className = "glass-card rounded-2xl p-4 border border-slate-800 transition-all";
    row.dataset.filename = file.name;
    row.innerHTML = `
      <div class="flex items-center justify-between gap-3">
        <div class="flex items-center space-x-3 min-w-0">
          <div class="w-8 h-8 rounded-lg bg-slate-900 border border-slate-800 flex items-center justify-center shrink-0 text-slate-400">
            <i data-lucide="file-up" class="w-4 h-4"></i>
          </div>
          <div class="min-w-0">
            <p class="text-xs font-semibold text-slate-200 truncate">${escapeHtml(file.name)}</p>
            <p class="text-[11px] text-slate-500 status-text mt-0.5">Queued in line…</p>
          </div>
        </div>
        <span class="status-badge text-[11px] font-semibold shrink-0 rounded-full px-2.5 py-0.5 bg-slate-900 border border-slate-800 text-slate-400">
          Queued
        </span>
      </div>
      <div class="mt-3 h-1.5 bg-slate-900 rounded-full overflow-hidden border border-slate-800/80">
        <div class="progress-fill h-full bg-gradient-to-r from-indigo-500 to-sky-400 transition-all duration-300 rounded-full" style="width: 5%"></div>
      </div>
    `;
    queue.prepend(row);
    if (window.lucide) lucide.createIcons({ root: row });
  }

  async function processSequentially(files) {
    for (const file of files) {
      const row = [...queue.children].find((r) => r.dataset.filename === file.name && !r.dataset.done);
      if (!row) continue;

      setRowState(
        row,
        "Reading, OCR & Gemini classifying…",
        "bg-sky-500/10 text-sky-400 border border-sky-500/20 animate-pulse",
        60
      );

      const formData = new FormData();
      formData.append("temp_file", file);

      try {
        const { status, data, ok } = await dvFetch("/api/upload/", { method: "POST", body: formData });
        row.dataset.done = "true";

        if (ok && data.ok) {
          row.dataset.docId = data.id; // Assign ID for dynamic removal
          applyResult(row, data);
        } else {
          setRowState(row, data.error || "Upload rejected.", "bg-rose-500/10 text-rose-400 border border-rose-500/20", 100);
        }
      } catch (err) {
        row.dataset.done = "true";
        setRowState(row, "Network error during upload.", "bg-rose-500/10 text-rose-400 border border-rose-500/20", 100);
      }
    }
  }

  function applyResult(row, doc) {
    const fill = row.querySelector(".progress-fill");
    fill.style.width = "100%";

    const map = {
      filed: ["Filed to SharePoint", "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"],
      needs_review: ["Needs Review", "bg-amber-500/10 text-amber-400 border border-amber-500/20"],
      failed: ["Failed", "bg-rose-500/10 text-rose-400 border border-rose-500/20"],
      processing: ["Processing", "bg-sky-500/10 text-sky-400 border border-sky-500/20 animate-pulse"],
    };

    const [label, classes] = map[doc.status] || ["Processed", "bg-slate-900 text-slate-300"];
    const badge = row.querySelector(".status-badge");
    badge.textContent = label;
    badge.className = `status-badge text-[11px] font-semibold shrink-0 rounded-full px-2.5 py-0.5 ${classes}`;

    const details = [];
    if (doc.category) details.push(doc.category);
    if (doc.company_name && doc.company_name !== "—") details.push(doc.company_name);
    if (doc.document_year) details.push(doc.document_year);

    row.querySelector(".status-text").textContent = details.length
      ? details.join(" · ")
      : (doc.ai_summary || doc.error_message || "Ingestion complete.");

    // Action buttons container
    const actions = document.createElement("div");
    actions.className = "flex items-center space-x-3 mt-1.5";

    if (doc.status === "needs_review" || doc.status === "failed") {
      const reviewBtn = document.createElement("button");
      reviewBtn.type = "button";
      reviewBtn.className = "text-xs font-semibold text-amber-400 hover:text-amber-300 flex items-center space-x-1";
      reviewBtn.innerHTML = `<span>Review & File</span><i data-lucide="arrow-right" class="w-3 h-3"></i>`;
      reviewBtn.onclick = () => window.openReviewModal(doc.id);
      actions.appendChild(reviewBtn);

      const discardBtn = document.createElement("button");
      discardBtn.type = "button";
      discardBtn.className = "text-xs font-semibold text-slate-400 hover:text-rose-400 flex items-center space-x-1";
      discardBtn.innerHTML = `<span>Discard</span>`;
      discardBtn.onclick = () => window.discardDocument(doc.id);
      actions.appendChild(discardBtn);
    } else if (doc.status === "filed") {
      const telemetryBtn = document.createElement("button");
      telemetryBtn.type = "button";
      telemetryBtn.className = "text-xs font-semibold text-indigo-400 hover:text-indigo-300 flex items-center space-x-1";
      telemetryBtn.innerHTML = `<span>View Telemetry</span><i data-lucide="arrow-right" class="w-3 h-3"></i>`;
      telemetryBtn.onclick = () => window.openDocModal(doc.id);
      actions.appendChild(telemetryBtn);
    }

    row.querySelector(".status-text").after(actions);
    if (window.lucide) lucide.createIcons({ root: row });
  }

  function setRowState(row, text, badgeClasses, width) {
    row.querySelector(".status-text").textContent = text;
    row.querySelector(".progress-fill").style.width = width + "%";
    const badge = row.querySelector(".status-badge");
    badge.className = `status-badge text-[11px] font-semibold shrink-0 rounded-full px-2.5 py-0.5 ${badgeClasses}`;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }
})();