// Adds trade AI analysis browser behavior.
(function () {
    var section = document.getElementById('ai-section');
    if (!section) return;

    var tradeId = section.getAttribute('data-trade-id');
    var analyzeBtn = document.getElementById('ai-btn');
    var reanalyzeBtn = document.getElementById('ai-reanalyze-btn');
    var retryBtn = document.getElementById('ai-retry-btn');

    var SIGNAL_COLOR = {
        CONTRARIAN: '#f59e0b',
        CONSENSUS: '#6ed1ff',
        CONVICTION: '#a78bfa',
        SPECULATIVE: '#f97316'
    };
    var RISK_COLOR = { LOW: '#3bcb93', MEDIUM: '#f4bb61', HIGH: '#f26b88' };

    function setDisplay(id, value) {
        var el = document.getElementById(id);
        if (el) el.style.display = value;
    }

    function setLoading(msg) {
        var loadingMsg = document.getElementById('ai-loading-msg');
        if (loadingMsg) loadingMsg.textContent = msg || 'Analyzing trade...';
        setDisplay('ai-loading', 'flex');
        setDisplay('ai-result', 'none');
        setDisplay('ai-model-loading', 'none');
        setDisplay('ai-unavailable', 'none');
    }

    function setButtonsDisabled(disabled) {
        if (analyzeBtn) analyzeBtn.disabled = disabled;
        if (reanalyzeBtn) reanalyzeBtn.disabled = disabled;
    }

    function showUnavailable(message) {
        var unavailableMessage = document.getElementById('ai-unavailable-message');
        if (message && unavailableMessage) {
            unavailableMessage.textContent = message;
        }
        setDisplay('ai-unavailable', 'block');
    }

    function csrfHeaders() {
        var token = window.getCsrfToken ? window.getCsrfToken() : null;
        return token ? { 'X-CSRF-Token': token } : {};
    }

    function addContextItem(parent, label, value) {
        var item = document.createElement('div');
        item.className = 'ai-ctx-item';
        var labelEl = document.createElement('span');
        labelEl.className = 'ai-ctx-label';
        labelEl.textContent = label;
        var valueEl = document.createElement('span');
        valueEl.className = 'ai-ctx-val';
        valueEl.textContent = value;
        item.appendChild(labelEl);
        item.appendChild(valueEl);
        parent.appendChild(item);
    }

    async function reanalyzeTrade() {
        setButtonsDisabled(true);
        setLoading('Clearing cached analysis...');
        try {
            await fetch('/api/trades/' + encodeURIComponent(tradeId) + '/ai-analysis/invalidate', {
                method: 'POST',
                headers: csrfHeaders()
            });
        } catch (_) {
            // Still try a fresh read; the cache may already be empty.
        }
        await loadTradeAnalysis();
    }

    async function loadTradeAnalysis() {
        setButtonsDisabled(true);
        setLoading('Analyzing trade...');

        try {
            var resp = await fetch('/api/trades/' + encodeURIComponent(tradeId) + '/ai-analysis');
            if (!resp.ok) {
                throw new Error('AI request failed with status ' + resp.status);
            }
            var data = await resp.json();
            setDisplay('ai-loading', 'none');

            if (data.error === 'model_loading') {
                setDisplay('ai-model-loading', 'block');
                setButtonsDisabled(false);
                return;
            }
            if (!data.available) {
                showUnavailable(data.message);
                setButtonsDisabled(false);
                return;
            }

            var sigEl = document.getElementById('ai-signal-badge');
            if (sigEl) {
                sigEl.textContent = data.signal || '-';
                sigEl.style.background = SIGNAL_COLOR[data.signal] || '#6b7280';
                sigEl.style.color = '#070b12';
            }

            var riskEl = document.getElementById('ai-risk-badge');
            if (riskEl) {
                riskEl.textContent = (data.risk || '-') + ' RISK';
                riskEl.style.background = RISK_COLOR[data.risk] || '#6b7280';
                riskEl.style.color = '#070b12';
            }

            var scoreEl = document.getElementById('ai-score-badge');
            if (scoreEl) {
                if (typeof data.score === 'number') {
                    scoreEl.textContent = 'MODEL ' + Math.round(data.score * 100) + '%';
                    scoreEl.style.background = '#1f2937';
                    scoreEl.style.color = '#e5e7eb';
                    if (typeof data.threshold === 'number') {
                        scoreEl.title = 'Flag line: ' + Math.round(data.threshold * 100) + '%';
                    }
                    scoreEl.style.display = 'inline-flex';
                } else {
                    scoreEl.style.display = 'none';
                }
            }

            var cacheEl = document.getElementById('ai-cache-badge');
            if (cacheEl) {
                if (data.from_cache) {
                    cacheEl.textContent = 'cached';
                    cacheEl.style.display = 'inline-flex';
                    if (reanalyzeBtn) reanalyzeBtn.style.display = 'inline-flex';
                } else {
                    cacheEl.style.display = 'none';
                    if (reanalyzeBtn) reanalyzeBtn.style.display = 'none';
                }
            }

            var providerText = '';
            if (data.provider) providerText = 'via ' + data.provider;
            if (data.model_version) providerText += providerText ? ' / ' + data.model_version : data.model_version;
            var providerEl = document.getElementById('ai-provider-label');
            if (providerEl) providerEl.textContent = providerText;

            document.getElementById('ai-verdict').textContent = data.verdict || '';
            document.getElementById('ai-price-insight').textContent = data.price_insight || '';
            document.getElementById('ai-behavior').textContent = data.behavior || '';

            var reasonBlock = document.getElementById('ai-reason-block');
            var reasonEl = document.getElementById('ai-reason');
            if (reasonBlock && reasonEl) {
                if (data.analysis_reason) {
                    reasonEl.textContent = data.analysis_reason;
                    reasonBlock.style.display = 'block';
                } else {
                    reasonBlock.style.display = 'none';
                }
            }

            var ctx = data.context;
            var grid = document.getElementById('ai-context-grid');
            if (ctx && grid) {
                grid.textContent = '';
                var firstBadge = ctx.is_first_trade_on_market ? 'First entry' : ctx.wallet_trades_on_this_market + ' prior trades here';
                var sizeLabel = ctx.size_vs_wallet_avg + 'x avg';
                addContextItem(grid, 'Market sentiment', ctx.market_yes_pct + '% YES - ' + ctx.market_sentiment);
                addContextItem(grid, 'Market depth', ctx.market_total_trades + ' trades / ' + ctx.market_unique_wallets + ' wallets');
                addContextItem(grid, 'Price vs consensus', ctx.price_vs_consensus);
                addContextItem(grid, 'Position size', '$' + ctx.trade_value + ' - ' + sizeLabel);
                if (typeof ctx.model_signal_score === 'number') {
                    addContextItem(grid, 'Local model score', Math.round(ctx.model_signal_score * 100) + '% signal');
                }
                addContextItem(grid, 'Market history', firstBadge);
                addContextItem(grid, 'Wallet bias', ctx.wallet_bias + ' (' + ctx.wallet_yes_bias_pct + '% YES overall)');
                if (ctx.wallet_has_track_record) {
                    var roiTxt = (typeof ctx.wallet_roi_pct === 'number') ? ', ' + (ctx.wallet_roi_pct > 0 ? '+' : '') + ctx.wallet_roi_pct + '% ROI' : '';
                    addContextItem(grid, 'Track record', ctx.wallet_win_rate_pct + '% win rate (' + ctx.wallet_markets_won + 'W/' + ctx.wallet_markets_lost + 'L' + roiTxt + ')');
                }
            }

            setDisplay('ai-result', 'block');
            if (analyzeBtn) analyzeBtn.textContent = 'Analyze again';
            setButtonsDisabled(false);
        } catch (err) {
            setDisplay('ai-loading', 'none');
            showUnavailable();
            setButtonsDisabled(false);
            console.error('AI analysis error:', err);
        }
    }

    if (analyzeBtn) analyzeBtn.addEventListener('click', loadTradeAnalysis);
    if (reanalyzeBtn) reanalyzeBtn.addEventListener('click', reanalyzeTrade);
    if (retryBtn) retryBtn.addEventListener('click', loadTradeAnalysis);

    // Auto-run when arriving via an "Analyze" deep link (?analyze=1) from a
    // trades table or dashboard event, and bring the AI card into view.
    try {
        var params = new URLSearchParams(window.location.search);
        if (params.get('analyze') === '1' && analyzeBtn && !analyzeBtn.disabled) {
            section.scrollIntoView({ behavior: 'smooth', block: 'start' });
            loadTradeAnalysis();
        }
    } catch (_) { /* deep-link auto-run is best-effort */ }
}());
