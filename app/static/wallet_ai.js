// Adds wallet AI summary browser behavior.
(function () {
    var section = document.getElementById('ai-summary-section');
    if (!section) return;

    var address = section.getAttribute('data-wallet-address');
    var btn = document.getElementById('ai-summary-btn');
    var loading = document.getElementById('ai-summary-loading');
    var result = document.getElementById('ai-summary-result');
    var unavail = document.getElementById('ai-summary-unavailable');
    var unavailMsg = document.getElementById('ai-summary-unavailable-message');
    var noTrades = document.getElementById('ai-summary-no-trades');

    function show(el, display) {
        if (el) el.style.display = display;
    }

    async function loadWalletSummary() {
        if (!btn || !address) return;

        btn.disabled = true;
        show(loading, 'flex');
        show(result, 'none');
        show(unavail, 'none');
        show(noTrades, 'none');

        try {
            var resp = await fetch('/api/wallets/' + encodeURIComponent(address) + '/ai-summary');
            var data = await resp.json();
            show(loading, 'none');

            if (!data.available) {
                if (data.reason === 'no_trades') {
                    show(noTrades, 'block');
                } else {
                    if (data.message && unavailMsg) {
                        unavailMsg.textContent = data.message;
                    }
                    show(unavail, 'block');
                }
                btn.disabled = false;
                return;
            }

            document.getElementById('ai-summary-text').textContent = data.summary || '';
            document.getElementById('ai-summary-meta').textContent = 'Based on ' + data.trade_count + ' most recent trade' + (data.trade_count !== 1 ? 's' : '');
            show(result, 'block');
            btn.textContent = 'Re-analyse';
            btn.disabled = false;
        } catch (_) {
            show(loading, 'none');
            show(unavail, 'block');
            btn.disabled = false;
        }
    }

    if (btn) btn.addEventListener('click', loadWalletSummary);
}());
