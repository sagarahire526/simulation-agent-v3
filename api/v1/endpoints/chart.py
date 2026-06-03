"""
Chart visualization endpoints.

  GET /api/v1/chart/{query_id}       — JSON chart data for a query
  GET /api/v1/chart/{query_id}/view  — HTML page rendering the chart via Highcharts
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

import services.db_service as db_svc

router = APIRouter(prefix="/chart", tags=["Chart"])


@router.get("/{query_id}")
def get_chart_data(query_id: str):
    """Return the raw chart JSON stored for this query."""
    row = db_svc.get_graph_by_query_id(query_id)
    if not row:
        raise HTTPException(status_code=404, detail="Query not found")
    if not row.get("graph"):
        raise HTTPException(status_code=404, detail="No chart data for this query")
    return row["graph"]


@router.get("/{query_id}/view", response_class=HTMLResponse)
def view_chart(query_id: str):
    """Serve a self-contained HTML page that renders the chart with Highcharts."""
    row = db_svc.get_graph_by_query_id(query_id)
    if not row:
        raise HTTPException(status_code=404, detail="Query not found")
    if not row.get("graph"):
        raise HTTPException(status_code=404, detail="No chart data for this query")

    title = (row.get("original_query") or "Chart Preview")[:120]

    return _CHART_HTML_TEMPLATE.replace("__QUERY_ID__", query_id).replace("__TITLE__", title)


_CHART_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__TITLE__</title>
  <script src="https://code.highcharts.com/highcharts.js"></script>
  <script src="https://code.highcharts.com/modules/gantt.js"></script>
  <script src="https://code.highcharts.com/modules/exporting.js"></script>
  <script src="https://code.highcharts.com/modules/export-data.js"></script>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f5f5; padding: 24px; color: #333;
    }
    .header { margin-bottom: 24px; }
    .header h1 { font-size: 20px; font-weight: 600; color: #1a1a1a; }
    .header p { font-size: 13px; color: #666; margin-top: 4px; }
    .charts-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(540px, 1fr));
      gap: 20px;
    }
    .chart-card {
      background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      padding: 16px; min-height: 400px;
    }
    .chart-card.gantt-card { grid-column: 1 / -1; min-height: 480px; }
    .rationale {
      margin-top: 20px; padding: 12px 16px; background: #f0f4ff;
      border-left: 3px solid #4a7cf7; border-radius: 4px; font-size: 13px; color: #555;
    }
    .error { color: #c0392b; text-align: center; padding: 60px 20px; }
    .chart-warn { font-size: 12px; color: #b9770e; padding: 8px; text-align: center; }
  </style>
</head>
<body>
  <div class="header">
    <h1>__TITLE__</h1>
    <p>Query ID: <code>__QUERY_ID__</code></p>
  </div>
  <div class="charts-grid" id="charts-grid"></div>
  <div id="rationale"></div>

  <script>
    // ---- Defensive helpers (every parse failure degrades silently, never aborts) ----

    // Parse an ISO date string (YYYY-MM-DD or with time) to Unix ms.
    // Returns null on any failure — caller skips the bad point.
    function _toMs(v) {
      if (v === null || v === undefined || v === '') return null;
      if (typeof v === 'number' && isFinite(v)) return v;     // already ms
      var t = Date.parse(String(v));
      return (isFinite(t)) ? t : null;
    }

    // Normalize a Gantt series.data array: convert start/end ISO strings to ms,
    // skip any point missing/invalid dates, ensure y is a number.
    function _normalizeGanttData(rawData) {
      var skipped = 0;
      var out = [];
      (rawData || []).forEach(function(p) {
        if (!p || typeof p !== 'object') { skipped++; return; }
        var s = _toMs(p.start);
        var e = _toMs(p.end);
        if (s === null || e === null) { skipped++; return; }
        if (e < s) { var tmp = s; s = e; e = tmp; }            // swap if reversed
        var y = (typeof p.y === 'number') ? p.y : 0;
        var point = Object.assign({}, p, { start: s, end: e, y: y });
        // Optional Gantt fields — pass through cleanly
        if (p.name) point.name = String(p.name);
        if (p.completed !== undefined && typeof p.completed === 'number') {
          point.completed = Math.max(0, Math.min(1, p.completed));
        }
        out.push(point);
      });
      return { data: out, skipped: skipped };
    }

    // Render one Highcharts (or Gantt) chart inside `container`.
    // Returns true on success, false if the spec was unrenderable.
    function _renderChart(container, spec) {
      // Pull the chart kind from either `spec.type` (preferred) or spec.chart.type.
      var type = String((spec && spec.type) || (spec && spec.chart && spec.chart.type) || 'column').toLowerCase();

      if (type === 'gantt') {
        // Gantt — normalize all series' data points (date parsing + skipping)
        var totalSkipped = 0;
        var ganttSeries = (spec.series || []).map(function(s) {
          var n = _normalizeGanttData(s.data);
          totalSkipped += n.skipped;
          return Object.assign({}, s, { data: n.data });
        }).filter(function(s) { return s.data.length > 0; });

        if (ganttSeries.length === 0) {
          container.innerHTML = '<p class="chart-warn">Gantt skipped — no valid date ranges in series data.</p>';
          return false;
        }

        try {
          Highcharts.ganttChart(container.id, {
            title:       { text: spec.title || '' },
            subtitle:    { text: spec.subtitle || '' },
            xAxis:       spec.xAxis || {},
            yAxis:       Object.assign({}, spec.yAxis || {}, {
              uniqueNames: true,
              title: (spec.yAxis && spec.yAxis.title) || { text: '' }
            }),
            series:      ganttSeries,
            legend:      spec.legend || { enabled: true },
            tooltip:     spec.tooltip || {
              pointFormat: '<b>{point.name}</b><br/>{point.start:%e %b %Y} → {point.end:%e %b %Y}'
            },
            plotOptions: Object.assign({ series: { dataLabels: { enabled: false } } }, spec.plotOptions || {}),
            credits:     { enabled: false },
          });
        } catch (e) {
          container.innerHTML = '<p class="chart-warn">Gantt render failed: ' + e.message + '</p>';
          return false;
        }

        if (totalSkipped > 0) {
          var w = document.createElement('div');
          w.className = 'chart-warn';
          w.textContent = '(' + totalSkipped + ' data point(s) skipped due to invalid dates)';
          container.appendChild(w);
        }
        return true;
      }

      // Non-Gantt charts: column / bar / line / pie / area / scatter / spline / areaspline
      try {
        Highcharts.chart(container.id, {
          chart:       { type: type },
          title:       { text: spec.title || '' },
          subtitle:    { text: spec.subtitle || '' },
          xAxis:       spec.xAxis || {},
          yAxis:       spec.yAxis || {},
          series:      spec.series || [],
          legend:      spec.legend || { enabled: true },
          tooltip:     spec.tooltip || {},
          plotOptions: spec.plotOptions || {},
          credits:     { enabled: false },
        });
        return true;
      } catch (e) {
        container.innerHTML = '<p class="chart-warn">Chart render failed: ' + e.message + '</p>';
        return false;
      }
    }

    fetch('http://127.0.0.1:8000/api/v1/chart/__QUERY_ID__')
      .then(function(r) { if (!r.ok) throw new Error(r.statusText); return r.json(); })
      .then(function(data) {
        var grid = document.getElementById('charts-grid');

        if (!data.charts || data.charts.length === 0) {
          grid.innerHTML = '<p class="error">No chart data available.</p>';
          return;
        }

        data.charts.forEach(function(spec, idx) {
          var card = document.createElement('div');
          var isGantt = String((spec && spec.type) || '').toLowerCase() === 'gantt';
          card.className = 'chart-card' + (isGantt ? ' gantt-card' : '');
          card.id = 'chart-' + idx;
          grid.appendChild(card);
          _renderChart(card, spec);
        });

        if (data.rationale) {
          document.getElementById('rationale').innerHTML =
            '<div class="rationale"><strong>Rationale:</strong> ' + data.rationale + '</div>';
        }
      })
      .catch(function(err) {
        document.getElementById('charts-grid').innerHTML =
          '<p class="error">Failed to load charts: ' + err.message + '</p>';
      });
  </script>
</body>
</html>
"""
