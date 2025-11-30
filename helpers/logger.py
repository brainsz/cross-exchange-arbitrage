"""
Logging helper module.
"""
import logging
import sys
import os

class TradingLogger:
    """Custom logger for trading bot."""
    
    def __init__(self, exchange: str, ticker: str, log_to_console: bool = True):
        self.exchange = exchange
        self.ticker = ticker
        
        os.makedirs("logs", exist_ok=True)
        self.log_filename = f"logs/{exchange}_{ticker}_client.log"
        
        self.logger = logging.getLogger(f"{exchange}_{ticker}_client")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        
        if log_to_console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)
            
    def log(self, message: str, level: str = "INFO"):
        """Log a message."""
        if level.upper() == "INFO":
            self.logger.info(message)
        elif level.upper() == "ERROR":
            self.logger.error(message)
        elif level.upper() == "WARNING":
            self.logger.warning(message)
        elif level.upper() == "DEBUG":
            self.logger.debug(message)
        else:
            self.logger.info(message)
