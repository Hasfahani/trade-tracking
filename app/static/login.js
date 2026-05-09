document.querySelector('form').addEventListener('submit', function () {
    var btn = this.querySelector('.login-submit');
    if (btn && !btn.disabled) {
        btn.disabled = true;
        btn.textContent = 'Signing in…';
    }
});
