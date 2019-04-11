import pandas as pd
import plotly.io as pio
from mpcontribs.config import mp_level01_titles
from mpcontribs.io.core.recdict import RecursiveDict

class MyRenderer(pio.base_renderers.MimetypeRenderer):
    def to_mimebundle(self, fig_dict):
        from plotly.io import to_html
        html = to_html(
            fig_dict,
            config=None,
            auto_play=False,
            include_plotlyjs=False,
            include_mathjax=False,
            post_script=None,
            full_html=False,
            animation_opts=None)
        return {'text/html': html}

my_renderer = MyRenderer()
pio.renderers['my_renderer'] = my_renderer
pio.renderers.render_on_display = True

class Plot(object):
    """class to hold and display single interactive graph/plot"""
    def __init__(self, config, table):
        self.config = config
        self.table = table

    def get_figure(self):
        from pandas import MultiIndex
        layout = dict(legend = dict(x=0.7, y=1), margin = dict(r=0, t=40))
        is_3d = isinstance(self.table.index, MultiIndex)
        if is_3d:
            import plotly.graph_objs as go
            from plotly import tools
            cols = self.table.columns
            ncols = 2 if len(cols) > 1 else 1
            nrows = len(cols)/ncols + len(cols)%ncols
            fig = tools.make_subplots(
                rows=nrows, cols=ncols, subplot_titles=cols, print_grid=False
            )
            for idx, col in enumerate(cols):
                series = self.table[col]
                z = [s.tolist() for _, s in series.groupby(level=0)]
                fig.append_trace(go.Heatmap(z=z, showscale=False), idx/ncols+1, idx%ncols+1)
            fig['layout'].update(layout)
        else:
            xaxis, yaxis = self.config['x'], self.config.get('y', None)
            yaxes = [yaxis] if yaxis is not None else \
                    [col for col in self.table.columns if col != xaxis]
            traces = []
            for axis in yaxes:
                if 'ₑᵣᵣ' not in axis:
                    tbl = self.table[[xaxis, axis]].replace('', pd.np.nan).dropna()
                    traces.append(dict(
                        x=tbl[xaxis].tolist(), y=tbl[axis].tolist(), name=axis
                    ))
            for trace in traces:
                err_axis = trace['name'] + 'ₑᵣᵣ'
                if err_axis in yaxes:
                    errors = self.table[err_axis].replace('', pd.np.nan).dropna()
                    trace['error_y'] = dict(
                        type='data', array=errors, visible=True
                    )
                    trace['mode'] = 'markers'
            layout.update(dict(
                xaxis = dict(title=xaxis),
                yaxis = dict(
                    title=self.config['table'],
                    type=self.config.get('yaxis', {}).get('type', '-')
                ),
                showlegend=self.config.get('showlegend', True)
            ))
            fig = dict(data=traces, layout=layout)

        return fig

    def _ipython_display_(self):
        fig = self.get_figure()
        is_3d = False # TODO decide by heatmap
        axis = 'z' if is_3d else 'x'
        npts = len(fig.get('data')[0][axis])
        static_fig = (is_3d and npts > 150) or (not is_3d and npts > 700)
        renderers = ['jupyterlab']
        renderers.append('png' if static_fig else 'my_renderer')
        pio.renderers.default = '+'.join(renderers)
        pio.show(fig)

class Plots(RecursiveDict):
    """class to hold and display multiple interactive graphs/plots"""
    def __init__(self, tables, plotconfs):
        super(Plots, self).__init__(
            (plotconf['table'], Plot(
                plotconf, tables[plotconf['table']]
            )) for plotconf in plotconfs.values()
        )

    def __str__(self):
        return 'plots: {}'.format(' '.join(self.keys()))

    def _ipython_display_(self):
        for name, plot in self.items():
            if plot:
                display_html('<h3>{}</h3>'.format(name), raw=True)
                display_html(plot)

class GraphicalData(RecursiveDict):
    """class to hold and display all interactive graphs/plots of a MPFile"""
    def __init__(self, document):
        from mpcontribs.io.core.components.tdata import TabularData
        tdata = TabularData(document)
        super(GraphicalData, self).__init__(
            (identifier, Plots(
                tdata[identifier], content[mp_level01_titles[2]]
            )) for identifier, content in document.items()
            if mp_level01_titles[2] in content
        )

    def __str__(self):
        return 'mp-ids: {}'.format(' '.join(self.keys()))

    def _ipython_display_(self):
        for identifier, plots in self.items():
            if identifier != mp_level01_titles[0] and plots:
                display_html('<h2>Interactive Plots for {}</h2>'.format(identifier), raw=True)
                display_html(plots)
