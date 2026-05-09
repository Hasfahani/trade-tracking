// Copy-to-clipboard buttons
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
            btn.textContent = 'Loading...';
        }
    });
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
