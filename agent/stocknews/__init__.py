"""AI stock news dashboard: the scheduled news agent.

Pipeline: collect market data and news (Finnhub, SEC EDGAR, RSS) -> let Claude on
Amazon Bedrock research and write the edition as validated JSON -> compose the
dashboard contract -> publish JSON to S3 (served by CloudFront) or a local folder.
"""

__version__ = "1.0.0"
