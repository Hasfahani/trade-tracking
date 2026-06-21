// Adds shared browser behavior for app pages.
document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', async function () {
        var text = button.getAttribute('data-copy');
        var original = button.dataset.copyLabel || button.textContent;
        try {
            await navigator.clipboard.writeText(text);
            button.textContent = 'Copied';
        } catch (_) {
            button.textContent = 'Copy failed';
        }
        window.setTimeout(function () { button.textContent = original; }, 1200);
    });
});

// Active nav highlighting
var currentPath = window.location.pathname;
document.querySelectorAll('.top-nav a').forEach(function (a) {
    var href = a.getAttribute('href');
    if (href === currentPath || (href !== '/' && currentPath.startsWith(href))) {
        a.classList.add('active');
    }
});

// Page loading bar
var loader = document.getElementById('page-loader');
function startLoader() {
    if (!loader) return;
    loader.style.transition = 'none';
    loader.style.width = '0';
    void loader.offsetWidth;
    loader.classList.add('is-loading');
}
document.querySelectorAll('a[href]').forEach(function (a) {
    var href = a.getAttribute('href');
    if (href && !href.startsWith('#') && !href.startsWith('mailto:') && !a.target) {
        a.addEventListener('click', startLoader);
    }
});

// Loading state: disable submit buttons on form submit to prevent double-click
document.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function () {
        startLoader();
        var btn = form.querySelector('button[type="submit"]:not([data-no-loading])');
        if (btn && !btn.disabled) {
            btn.disabled = true;
            btn.dataset.orig = btn.textContent;
            btn.textContent = 'Loading…';
            btn.classList.add('btn-loading');
        }
    });
});

// Generic submit confirmation hook (replaces inline onsubmit confirm handlers)
document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
        var msg = form.getAttribute('data-confirm') || 'Are you sure?';
        if (!window.confirm(msg)) {
            e.preventDefault();
        }
    }, true);
});

// Hamburger menu toggle
var navToggle = document.querySelector('.nav-toggle');
var topNav = document.getElementById('top-nav');
if (navToggle && topNav) {
    navToggle.addEventListener('click', function (e) {
        e.stopPropagation();
        var open = topNav.classList.toggle('nav-open');
        navToggle.setAttribute('aria-expanded', String(open));
        navToggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    });
    document.addEventListener('click', function (e) {
        if (!navToggle.contains(e.target) && !topNav.contains(e.target)) {
            topNav.classList.remove('nav-open');
            navToggle.setAttribute('aria-expanded', 'false');
            navToggle.setAttribute('aria-label', 'Open menu');
        }
    });
}

// CSRF: inject token into every POST form from the cookie set by the server
(function () {
    function getCookie(name) {
        var m = document.cookie.match('(?:^|;)\\s*' + name + '=([^;]*)');
        return m ? decodeURIComponent(m[1]) : null;
    }
    window.getCsrfToken = function () {
        return getCookie('csrftoken');
    };
    var token = getCookie('csrftoken');
    if (token) {
        document.querySelectorAll('form[method="post"], form[method="POST"]').forEach(function (form) {
            if (!form.querySelector('input[name="csrf_token"]')) {
                var input = document.createElement('input');
                input.type = 'hidden';
                input.name = 'csrf_token';
                input.value = token;
                form.appendChild(input);
            }
        });
    }
}());

// Flash message: close button + auto-dismiss after 9 seconds
var flash = document.querySelector('.flash');
if (flash) {
    var closeBtn = document.createElement('button');
    closeBtn.className = 'flash-close';
    closeBtn.setAttribute('aria-label', 'Dismiss');
    closeBtn.textContent = 'x';
    closeBtn.addEventListener('click', function () { flash.remove(); });
    flash.appendChild(closeBtn);
    setTimeout(function () {
        flash.style.transition = 'opacity 0.6s ease';
        flash.style.opacity = '0';
        setTimeout(function () { flash.remove(); }, 650);
    }, 9000);
}

// Toast notifications
var _toastContainer = null;
function _getToastContainer() {
    if (!_toastContainer) {
        _toastContainer = document.createElement('div');
        _toastContainer.className = 'toast-container';
        _toastContainer.setAttribute('aria-live', 'polite');
        document.body.appendChild(_toastContainer);
    }
    return _toastContainer;
}

function showToast(message, type) {
    type = type || 'info';
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.textContent = message;
    _getToastContainer().appendChild(el);
    requestAnimationFrame(function () {
        requestAnimationFrame(function () { el.classList.add('toast-in'); });
    });
    setTimeout(function () {
        el.classList.remove('toast-in');
        setTimeout(function () { el.remove(); }, 300);
    }, 4000);
}

// Settings form validation (client-side)
var settingsForm = document.querySelector('form[action="/settings"]');
if (settingsForm) {
    var TOKEN_RE = /^\d+:[\w\-]+$/;
    var CHAT_RE = /^-?\d+$/;
    settingsForm.addEventListener('submit', function (e) {
        var tokenInput = settingsForm.querySelector('[name="telegram_bot_token"]');
        var chatInput = settingsForm.querySelector('[name="telegram_chat_id"]');
        var submitBtn = settingsForm.querySelector('button[type="submit"]');
        function clearLoadingState() {
            if (!submitBtn) return;
            submitBtn.disabled = false;
            if (submitBtn.dataset.orig) {
                submitBtn.textContent = submitBtn.dataset.orig;
                delete submitBtn.dataset.orig;
            }
            submitBtn.classList.remove('btn-loading');
        }
        var tokenVal = tokenInput ? tokenInput.value.trim() : '';
        var chatVal = chatInput ? chatInput.value.trim() : '';
        if (tokenVal && !TOKEN_RE.test(tokenVal)) {
            e.preventDefault();
            e.stopImmediatePropagation();
            clearLoadingState();
            showToast('Bot token format is invalid. Expected: 123456789:AAExj7...', 'error');
            if (tokenInput) tokenInput.focus();
            return;
        }
        if (chatVal && !CHAT_RE.test(chatVal)) {
            e.preventDefault();
            e.stopImmediatePropagation();
            clearLoadingState();
            showToast('Chat ID must be a number (e.g. 8708428862 or -100123456).', 'error');
            if (chatInput) chatInput.focus();
        }
    }, true);
}

// "Add a wallet" focus shortcut — replaces inline onclick in template
document.querySelectorAll('.js-focus-address').forEach(function (el) {
    el.addEventListener('click', function (e) {
        e.preventDefault();
        var target = document.querySelector('[name="address"]');
        if (target) target.focus();
    });
});

// Staleness indicator: show how long ago the last sync completed
function _relativeTime(isoStr) {
    if (!isoStr) return 'Never';
    var diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 60000);
    if (diff < 1) return 'Just now';
    if (diff < 60) return diff + ' min ago';
    var h = Math.floor(diff / 60);
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
}

function _updateStaleness() {
    var el = document.getElementById('staleness-ts');
    if (!el) return;
    fetch('/api/last-sync')
        .then(function (r) { return r.json(); })
        .then(function (data) { el.textContent = _relativeTime(data.iso); })
        .catch(function () {});
}

_updateStaleness();
setInterval(_updateStaleness, 60000);

// Sticky bars: reveal an element once the page scrolls past data-sticky-after px
document.querySelectorAll('[data-sticky-after]').forEach(function (el) {
    var threshold = parseInt(el.getAttribute('data-sticky-after'), 10) || 200;
    var ticking = false;
    function update() {
        var show = window.scrollY > threshold;
        el.classList.toggle('is-visible', show);
        el.setAttribute('aria-hidden', show ? 'false' : 'true');
        ticking = false;
    }
    window.addEventListener('scroll', function () {
        if (!ticking) {
            window.requestAnimationFrame(update);
            ticking = true;
        }
    }, { passive: true });
    update();
});

// Collapsible panels (mobile filters): toggle [data-collapse-target] visibility
document.querySelectorAll('[data-collapse-toggle]').forEach(function (btn) {
    var target = document.getElementById(btn.getAttribute('data-collapse-toggle'));
    if (!target) return;
    btn.addEventListener('click', function () {
        var open = target.classList.toggle('is-open');
        btn.setAttribute('aria-expanded', String(open));
    });
});
