import csv
import datetime
import json
import logging
import os.path
import urllib.parse
from argparse import ArgumentParser
from statistics import mean
from typing import Dict, List, TypedDict, Union
from urllib.request import urlopen

import numpy as np
from matplotlib import pyplot as plt
from numpy.lib.stride_tricks import sliding_window_view
from ruamel.yaml import CommentedMap
from sklearn.linear_model import LinearRegression

from analysis.config import load_config
from analysis.shared_parsers import extract_year_ticks
from models.bass_diffusion import BassDiffusionModel

StatsDict = TypedDict('StatsDict',
                      {'crawl': str, 'mimetype_detected': str, 'pages': int, 'urls': int, 'pct_pages_per_crawl': float})
StatsDictTable = List[StatsDict]

Crawl, PageCount = str, int
MimeStats = Dict[str, int]
MimeType = str
MimeDict = Dict[MimeType, List[MimeStats]]

ModelStats = List[Dict[str, Union[str, float]]]


def main(config: CommentedMap) -> int:
    start = datetime.datetime.now()

    crawl_cfg = config['data']['common_crawl']

    # Get the collection metadata
    with urlopen(crawl_cfg['collection_url']) as f:
        coll_info = json.loads(f.read().decode('utf-8'))

    # Get the pre-aggregated statistics from the Common Crawl repository
    response = urlopen(crawl_cfg['stats_url'])
    lines = [line.decode('utf-8') for line in response.readlines()]
    contents = csv.DictReader(lines)
    stats = [line for line in contents]

    typed_stats = parse_csv(stats)
    declining = filter_declining(typed_stats)
    model_stats = analyse(declining, coll_info, config)
    model_stats = sorted(model_stats, key=lambda row: float(row['Ratio Bass over lineair']))

    with open(os.path.join(crawl_cfg['csv_output_dir'], 'bass_vs_linear_avg_errors.csv'), 'wt') as f:
        writer = csv.DictWriter(f, fieldnames=model_stats[0].keys())
        writer.writeheader()
        writer.writerows(model_stats)

    logging.info(json.dumps(model_stats, indent=2))

    logging.info(f'Script took {datetime.datetime.now() - start}')
    return 0


def parse_csv(stats: List[Dict[str, str]]) -> StatsDictTable:
    """
    Converts str dict values to types appropriate for the StatsDictTable format.

    :param stats: A list of raw Common Crawl statistics csv values from a csv.DictReader

    :return: A list of dictionaries with parsed string, int and float values
    """
    stats_dict = []
    for row in stats:
        stats_dict.append(StatsDict(
            crawl=str(row['crawl']),
            mimetype_detected=str(row['mimetype_detected']),
            pages=int(row['pages']),
            urls=int(row['urls']),
            pct_pages_per_crawl=float(row['%pages/crawl']),
        ))

    return stats_dict


def filter_declining(typed_stats: StatsDictTable) -> dict[str, list[dict[str, int]]]:
    """
    Filters the list of statistics for MIME types that decline over the last year

    :param typed_stats: a list of dictionaries with typed values

    :return: a dictionary of mime types with declining counts, with usage
    """
    declining_mime_types: dict[str, list[dict[str, int]]] = {}

    # First: "de-normalize" the table into a nested dictionary of mime types with page counts per crawl
    # This is easier to handle: we want to analyse statistics per mime type, over the years
    mime_sorted_stats = sorted(typed_stats, key=lambda r: (r['mimetype_detected'], r['crawl']))
    # Skip under-specified mime types
    mime_sorted_stats = [row for row in mime_sorted_stats
                         if row['mimetype_detected'] != '<unknown>' and row['mimetype_detected'] != '<other>']

    for row in mime_sorted_stats:
        declining_mime_types.setdefault(row['mimetype_detected'], [])
        usage_stat_ = str(config['data']['common_crawl']['usage_stat'])
        declining_mime_types[row['mimetype_detected']].append(
            {row['crawl']: row[usage_stat_]}  # type: ignore
        )

    mime_types = list(declining_mime_types.keys())
    mime_declines: list[dict[str, str | int]] = []

    for mime_type in mime_types:
        crawl_stats = declining_mime_types[mime_type]
        # Calculate window averages of three crawls over the crawl stats
        stats_values = [list(stat.values())[0] for stat in crawl_stats]
        windows = sliding_window_view(stats_values, 3)
        window_averages = [np.mean(window) for window in windows]

        # Drop zero-values from mime types that are no longer used
        while window_averages[-1] == 0.:
            window_averages.pop()

        num_crawls = 12
        last_usage_percentages = window_averages[-num_crawls:]
        diffs = [pct[1] - pct[0] for pct in sliding_window_view(last_usage_percentages, 2)]
        avg_increase = np.mean(diffs)

        # Now that we have fitted a simple regression line, the filter is simple: a positive coefficient means growth,
        # a negative number indicates decline
        if avg_increase >= 0:
            del declining_mime_types[mime_type]
        else:
            mime_declines.append({'mime_type': mime_type, 'avg_increase': avg_increase})

    mime_declines = sorted(mime_declines, key=lambda x: x['avg_increase'])
    logging.info(f'Largest declines: {json.dumps(mime_declines[0:10], indent=2)}')

    return declining_mime_types


def analyse(stats: MimeDict, collection_metadata: List[Dict[str, str]], config: CommentedMap) -> ModelStats:
    error_stats: ModelStats = []
    # Extract out shorthand for long dict value
    cc_cfg = config['data']['common_crawl']

    x_axis_labels = extract_year_ticks([entry['id'] for entry in reversed(collection_metadata)])

    for mime_type, usage_values in stats.items():
        usage_per_crawl = [list(row.values())[0] for row in usage_values]
        year_labels = x_axis_labels[-len(usage_per_crawl):]

        # Extract out the index for the test crawls
        test_crawls_idx = -cc_cfg['num_test_crawls']
        train_values = usage_per_crawl[:test_crawls_idx]

        all_times = np.array(range(len(usage_per_crawl)))
        bass_train_times = all_times[:test_crawls_idx]
        bass_test_times = all_times[test_crawls_idx:]

        # Fit the Bass model
        bass_model = BassDiffusionModel()
        bass_model.fit(times=bass_train_times, sales=np.array(train_values))

        # Project Bass model "sales"
        bass_fitted_values = bass_model.sales_at_time(bass_model.bass_parameters, bass_train_times)
        bass_projected_ndarray = bass_model.sales_at_time(bass_model.bass_parameters, bass_test_times)
        assert isinstance(bass_projected_ndarray, np.ndarray)
        bass_projected_values = bass_projected_ndarray.tolist()

        # Constrain the linear model to only the values from the highest value onwards
        max_idx = train_values.index(max(train_values))
        linear_train_times = np.expand_dims(all_times[max_idx:test_crawls_idx], axis=1)
        linear_train_values = usage_per_crawl[max_idx:test_crawls_idx]
        linear_test_times = np.expand_dims(all_times[test_crawls_idx:], axis=1)

        linear_model = LinearRegression()
        linear_model.fit(X=linear_train_times, y=linear_train_values)
        linear_fitted_values = linear_model.predict(linear_train_times)
        linear_projected_values = linear_model.predict(linear_test_times)

        # Calculate accuracy in average error
        actual_test_values = list(usage_per_crawl[test_crawls_idx:])
        assert isinstance(actual_test_values,  list)
        assert isinstance(bass_projected_values, list)

        bass_error = [abs(actual - predicted) for actual, predicted
                      in zip(actual_test_values, bass_projected_values)]
        linear_error = [abs(actual - predicted) for actual, predicted
                        in zip(actual_test_values, linear_projected_values)]

        error_stats.append({
            'Mime type': mime_type,
            'Bass gemiddelde fout': int(mean(bass_error)),
            'Linear gemiddelde fout': int(mean(linear_error)),
            'Ratio Bass over lineair': f'{int(mean(bass_error)) / int(mean(linear_error)):.2g}'
        })

        # Plot if marked so in configuration
        if mime_type in cc_cfg['mime_plots']:
            plt.plot(
                # Actual values
                all_times, usage_per_crawl,
                # Bass model predictions from data that the model has seen
                bass_train_times, bass_fitted_values,
                # Bass model predictions from data that the model has not seen
                bass_test_times, bass_projected_values,
                # Linear model predictions from data that the model has seen
                linear_train_times, linear_fitted_values,
                # Linear model predictions that the model has not yet seen
                linear_test_times, linear_projected_values,
            )
            plt.xticks(all_times, year_labels)
            plt.title(f"Common Crawl {cc_cfg['usage_stat']} per crawl voor {mime_type}")
            plt.legend([
                cc_cfg['usage_stat'].capitalize(),
                'Bass fit',
                'Bass test',
                'Lineair fit',
                'Lineair test',
            ])
            plt.savefig(f'images/common_crawl/{urllib.parse.quote_plus(mime_type)}.png')
            plt.show()

    return error_stats


if __name__ == '__main__':
    parser = ArgumentParser('Performs the Common Crawl MIME type usage-over-time analysis')
    parser.add_argument('-c', '--config', default='config.yaml')

    args = parser.parse_args()
    config = load_config(args.config)
    raise SystemExit(main(config))
