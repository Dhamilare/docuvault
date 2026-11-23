/**
 * DocuVault AI — Unified Light-Theme Client Engine (app.js)
 * Single source of truth for CSRF, dvFetch, toasts, modals, and document actions.
 */

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}
window.escapeHtml = escapeHtml;

function getCsrfToken() {
  if (window.CSRF_TOKEN) return window.CSRF_TOKEN;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.startsWith("csrftoken=")) {
        return decodeURIComponent(cookie.substring("csrftoken=".length));
      }
    }
  }
  return document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
}

async function dvFetch(url, options = {}) {
  const opts = {
    method: options.method || "GET",
    headers: {
      "X-Requested-With": "XMLHttpRequest",
      ...(options.headers || {}),
    },
    body: options.body,
  };

  if (opts.method !== "GET") {
    const token = getCsrfToken();
    if (token) opts.headers["X-CSRFToken"] = token;
  }

  const res = await fetch(url, opts);
  const contentType = res.headers.get("content-type") || "";
  let data = {};
  try {
    data = contentType.includes("application/json") ? await res.json() : await res.text();
  } catch (e) {}

  if (res.status === 401 && data && data.login_url) {
    dvToast("Session expired. Redirecting to sign in…", "warning");
    setTimeout(() => { window.location.href = data.login_url; }, 900);
  }

  return { status: res.status, data, ok: res.ok };
}
window.dvFetch = dvFetch;

// =============================================================================
// Modal Controllers
// =============================================================================

function getModalRoot() {
  return document.getElementById("modal-container") || document.getElementById("modal-root");
}

function showModalLoading(text) {
  const root = getModalRoot();
  if (!root) return;

  root.innerHTML = `
    <div class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/45 backdrop-blur-sm">
      <div class="modal-panel bg-white rounded-2xl px-6 py-4 flex items-center space-x-3 text-slate-700 text-xs font-semibold shadow-2xl border border-slate-200">
        <svg class="animate-spin w-4 h-4 text-indigo-600" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
        </svg>
        <span>${escapeHtml(text)}</span>
      </div>
    </div>
  `;
  root.classList.remove("hidden");
  root.classList.add("flex");
  document.body.style.overflow = "hidden";
}

function closeDocModal() {
  const root = getModalRoot();
  if (root) {
    root.innerHTML = "";
    root.classList.add("hidden");
    root.classList.remove("flex");
  }
  document.body.style.overflow = "";
}
window.closeDocModal = closeDocModal;

async function openDocModal(docId) {
  const root = getModalRoot();
  if (!root) return;

  showModalLoading("Loading document details…");

  try {
    const res = await fetch(`/api/documents/${docId}/detail/`, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (!res.ok) {
      dvToast("Failed to load document details.", "error");
      closeDocModal();
      return;
    }
    root.innerHTML = await res.text();
    if (window.lucide) lucide.createIcons({ root });
  } catch (err) {
    dvToast("Network error fetching document details.", "error");
    closeDocModal();
  }
}
window.openDocModal = openDocModal;

async function openReviewModal(docId) {
  const root = getModalRoot();
  if (!root) return;

  showModalLoading("Opening verification workspace…");

  try {
    const res = await fetch(`/api/documents/${docId}/review/`, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (!res.ok) {
      dvToast("Failed to load review form.", "error");
      closeDocModal();
      return;
    }

    root.innerHTML = await res.text();
    if (window.lucide) lucide.createIcons({ root });

    // Date picker -> Year dropdown auto-sync
    const dateInput = root.querySelector("#id_document_date");
    const yearSelect = root.querySelector("#id_document_year");
    if (dateInput && yearSelect) {
      dateInput.addEventListener("change", (e) => {
        if (!e.target.value) return;
        const year = e.target.value.split("-")[0];
        if (yearSelect.querySelector(`option[value="${year}"]`)) {
          yearSelect.value = year;
        }
      });
    }

    const form = root.querySelector("#review-doc-form");
    const errorBox = root.querySelector("#review-form-errors");
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (errorBox) {
          errorBox.classList.add("hidden");
          errorBox.textContent = "";
        }

        const submitBtn = form.querySelector('button[type="submit"]');
        const origHtml = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = `<span>Uploading &amp; Syncing ECM…</span>`;

        try {
          const { data, ok } = await dvFetch(form.action, {
            method: "POST",
            body: new FormData(form),
          });

          if (ok && data.ok) {
            dvToast(
              data.status === "filed" ? "Document filed to SharePoint." : "Changes saved.",
              "success"
            );
            closeDocModal();
            if (typeof window.dvRefreshList === "function") {
              window.dvRefreshList();
            } else {
              setTimeout(() => window.location.reload(), 350);
            }
          } else {
            const msg = (data && data.error) || "SharePoint filing failed.";
            if (errorBox) {
              errorBox.textContent = msg;
              errorBox.classList.remove("hidden");
            } else {
              dvToast(msg, "error");
            }
            submitBtn.disabled = false;
            submitBtn.innerHTML = origHtml;
          }
        } catch (err) {
          dvToast("Network error filing document.", "error");
          submitBtn.disabled = false;
          submitBtn.innerHTML = origHtml;
        }
      });
    }
  } catch (err) {
    dvToast("Network error opening review modal.", "error");
    closeDocModal();
  }
}
window.openReviewModal = openReviewModal;

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDocModal();
});

// =============================================================================
// Document Actions (Discard, Retry AI, Retry Metadata)
// =============================================================================

async function deleteDoc(target, optionalId) {
  let docId = optionalId;
  let element = null;

  if (typeof target === "number" || typeof target === "string") {
    docId = target;
  } else if (target && target.nodeType) {
    element = target;
    docId = optionalId || target.dataset.id || target.dataset.docId;
  }
  if (!docId) return;

  if (!confirm("Discard this document? The transient scan file will be permanently removed.")) return;

  const btn = element ? element.closest("button") : null;
  const originalHtml = btn ? btn.innerHTML : null;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="text-rose-600">Discarding…</span>`;
  }

  try {
    const { data, ok } = await dvFetch(`/api/documents/${docId}/delete/`, { method: "POST" });
    if (ok && data.ok) {
      dvToast("Document discarded.", "info");
      closeDocModal();
      document
        .querySelectorAll(`[data-doc-id="${docId}"], #doc-row-${docId}, #doc-card-${docId}`)
        .forEach((el) => {
          el.classList.add("opacity-0", "scale-95", "transition-all", "duration-200");
          setTimeout(() => el.remove(), 200);
        });
    } else {
      dvToast((data && data.error) || "Failed to discard document.", "error");
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    }
  } catch (err) {
    dvToast("Network error while discarding document.", "error");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalHtml;
    }
  }
}
window.deleteDoc = deleteDoc;
window.discardDocument = deleteDoc;

async function retryDoc(docId) {
  try {
    const { data, ok } = await dvFetch(`/api/documents/${docId}/retry/`, { method: "POST" });
    if (ok && data.ok) {
      dvToast("Document re-queued for AI classification.", "info");
      closeDocModal();
      if (typeof window.dvRefreshList === "function") window.dvRefreshList();
      else setTimeout(() => window.location.reload(), 600);
    } else {
      dvToast((data && data.error) || "Retry failed.", "error");
    }
  } catch (err) {
    dvToast("Network error initiating retry.", "error");
  }
}
window.retryDoc = retryDoc;

async function retryMetadataSync(docId) {
  try {
    const { data, ok } = await dvFetch(`/api/documents/${docId}/retry-metadata/`, { method: "POST" });
    if (ok && data.ok) {
      dvToast("SharePoint ECM columns synced.", "success");
      openDocModal(docId);
    } else {
      dvToast((data && data.error) || "Metadata sync failed.", "error");
    }
  } catch (err) {
    dvToast("Network error syncing metadata.", "error");
  }
}
window.retryMetadataSync = retryMetadataSync;

// =============================================================================
// Light-Theme Toast Notifications
// =============================================================================

function dvToast(message, variant = "info") {
  const host = document.getElementById("toast-host");
  if (!host) return;

  const palettes = {
    info: "bg-white border-slate-200 text-slate-800 shadow-slate-900/10",
    success: "bg-emerald-50 border-emerald-200 text-emerald-900 shadow-emerald-900/10",
    error: "bg-rose-50 border-rose-200 text-rose-900 shadow-rose-900/10",
    warning: "bg-amber-50 border-amber-200 text-amber-900 shadow-amber-900/10",
  };

  const icons = {
    info: '<i data-lucide="info" class="w-4 h-4 text-indigo-600 shrink-0 mt-0.5"></i>',
    success: '<i data-lucide="check-circle-2" class="w-4 h-4 text-emerald-600 shrink-0 mt-0.5"></i>',
    error: '<i data-lucide="alert-octagon" class="w-4 h-4 text-rose-600 shrink-0 mt-0.5"></i>',
    warning: '<i data-lucide="alert-triangle" class="w-4 h-4 text-amber-600 shrink-0 mt-0.5"></i>',
  };

  const el = document.createElement("div");
  el.className = `toast pointer-events-auto border ${palettes[variant] || palettes.info} rounded-xl shadow-xl px-4 py-3 text-xs font-semibold flex items-start space-x-2.5 transition-all duration-300`;
  el.innerHTML = `
    ${icons[variant] || icons.info}
    <span class="flex-1 leading-relaxed">${escapeHtml(message)}</span>
    <button class="text-slate-400 hover:text-slate-700 transition p-0.5 ml-1 leading-none text-sm" aria-label="Dismiss">&times;</button>
  `;

  el.querySelector("button").addEventListener("click", () => {
    el.classList.add("opacity-0", "translate-y-2");
    setTimeout(() => el.remove(), 220);
  });

  host.appendChild(el);
  if (window.lucide) lucide.createIcons({ root: el });

  setTimeout(() => {
    if (el.parentNode) {
      el.classList.add("opacity-0", "translate-y-2");
      setTimeout(() => el.remove(), 220);
    }
  }, 5000);
}
window.dvToast = dvToast;