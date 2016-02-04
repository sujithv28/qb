import click
from pyspark import SparkConf, SparkContext

from util.environment import QB_QUESTION_DB, QB_GUESS_DB, QB_SPARK_MASTER
from util.constants import FEATURE_NAMES
import extract_features as ef

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])


@click.group(context_settings=CONTEXT_SETTINGS)
def spark():
    pass


def create_spark_context(app_name="Quiz Bowl", lm_memory=False):
    spark_conf = SparkConf()
    if lm_memory:
        spark_conf = spark_conf.set('spark.max.cores', 12).set('spark.executor.cores', 12)
    return SparkContext(appName=app_name, master=QB_SPARK_MASTER, conf=spark_conf)


@spark.command()
@click.argument('features', nargs=-1, type=click.Choice(FEATURE_NAMES), required=True)
@click.option('--lm-memory', is_flag=True)
def extract_features(**kwargs):
    sc = create_spark_context(
        app_name='Quiz Bowl: ' + ' '.join(kwargs['features']),
        lm_memory=kwargs['lm_memory'])
    ef.spark_execute(sc, kwargs['features'], QB_QUESTION_DB, QB_GUESS_DB)


@spark.command()
def merge_features(**kwargs):
    pass


if __name__ == '__main__':
    spark()