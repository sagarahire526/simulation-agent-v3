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
  <script src="https://code.highcharts.com/modules/exporting.js"></script>
  <script src="https://code.highcharts.com/modules/export-data.js"></script>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f5f5; padding: 24px; color: #333;
    }
    .header { margin-bottom: 20px; }
    .header h1 { font-size: 18px; font-weight: 600; color: #1a1a1a; }
    .header p { font-size: 13px; color: #666; margin-top: 4px; }
    #chart-container {
      background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      padding: 16px; min-height: 420px;
    }
    .rationale {
      margin-top: 16px; padding: 12px 16px; background: #f0f4ff;
      border-left: 3px solid #4a7cf7; border-radius: 4px; font-size: 13px; color: #555;
    }
    .error { color: #c0392b; text-align: center; padding: 60px 20px; }
  </style>
</head>
<body>
  <div class="header">
    <h1>__TITLE__</h1>
    <p>Query ID: <code>__QUERY_ID__</code></p>
  </div>
  <div id="chart-container"></div>
  <div id="rationale"></div>

  <script>
    fetch('http://127.0.0.1:8000/api/v1/chart/__QUERY_ID__')
      .then(r => { if (!r.ok) throw new Error(r.statusText); return r.json(); })
      .then(data => {
        if (!data.charts || data.charts.length === 0) {
          document.getElementById('chart-container').innerHTML =
            '<p class="error">No chart data available.</p>';
          return;
        }
        const spec = data.charts[0];
        Highcharts.chart('chart-container', {
          chart:       { type: spec.type || 'column' },
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
        if (data.rationale) {
          document.getElementById('rationale').innerHTML =
            '<div class="rationale"><strong>Rationale:</strong> ' + data.rationale + '</div>';
        }
      })
      .catch(err => {
        document.getElementById('chart-container').innerHTML =
          '<p class="error">Failed to load chart: ' + err.message + '</p>';
      });
  </script>
</body>
</html>
"""
