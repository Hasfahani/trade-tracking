// Summary: Adds browser behavior for the login page.
// Details: It adds small browser interactions for the page without needing a separate frontend build system.
document.querySelector('form').addEventListener('submit', function () {
    var btn = this.querySelector('.login-submit');
    if (btn && !btn.disabled) {
        btn.disabled = true;
        btn.textContent = 'Signing in…';
    }
});
