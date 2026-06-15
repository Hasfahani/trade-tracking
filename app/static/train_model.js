// Adds browser behavior for model training.
(function () {
    var panel = document.getElementById('train-progress');
    if (!panel) return;
    var statusLine = document.getElementById('train-status-line');
    var metricsLine = document.getElementById('train-metrics');

    function fmt(value) {
        return (typeof value === 'number') ? value.toFixed(4) : '—';
    }

    function render(status) {
        if (status.state === 'running') {
            var loss = (typeof status.train_mse === 'number') ? ' — train loss ' + status.train_mse.toFixed(6) : '';
            statusLine.textContent = 'Training… epoch ' + status.epoch + '/' + status.total_epochs + loss;
            statusLine.className = '';
        } else if (status.state === 'done' && status.result) {
            var m = status.result.test_metrics || {};
            statusLine.textContent = 'Training complete (' + status.result.n_train + ' train / ' + status.result.n_test + ' test rows). Model reloaded.';
            statusLine.className = '';
            var thresholdLabel = (typeof status.result.threshold === 'number')
                ? ' (threshold ' + fmt(status.result.threshold) + ')'
                : ' (threshold 0.5)';
            metricsLine.textContent = 'Test base rate ' + fmt(m.base_rate) +
                ' · ROC-AUC ' + fmt(m.roc_auc) +
                ' · PR-AUC ' + fmt(m.pr_auc) +
                ' · precision ' + fmt(m.precision_at_threshold !== undefined ? m.precision_at_threshold : m.precision_at_0_5) +
                ' · recall ' + fmt(m.recall_at_threshold !== undefined ? m.recall_at_threshold : m.recall_at_0_5) +
                ' · F1 ' + fmt(m.f1_at_threshold !== undefined ? m.f1_at_threshold : m.f1_at_0_5) + thresholdLabel;
        } else if (status.state === 'error') {
            statusLine.textContent = 'Training failed: ' + (status.error || 'unknown error');
            statusLine.className = 'flash flash-error';
        }
    }

    function poll() {
        fetch('/admin/train-model/status', { headers: { 'Accept': 'application/json' } })
            .then(function (response) { return response.json(); })
            .then(function (status) {
                render(status);
                if (status.state === 'running') {
                    setTimeout(poll, 2000);
                }
            })
            .catch(function () { setTimeout(poll, 5000); });
    }

    if (panel.dataset.state === 'running') {
        poll();
    }
})();
