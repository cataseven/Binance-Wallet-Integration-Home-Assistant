# Binance Integration for Home Assistant

This integration allows you to add cryptocurrency prices and wallet balances from your Binance account to Home Assistant.

## Installation

Download from HACS or 

Copy paste files to your HA folder: custom_components/binance/ or

Add custom repo from HACS via https://github.com/cataseven/Home-Assitant-Binance-Integration.git

Clean your browser cache, restart HA and...

1. Create an API key and API secret for your Binance account. <strong><span style="color: red;">IMPORTANT</span></strong> Please only use Read Only API. If you do not want to create sensor for your Wallet Balance do not check "Permits Universal Transfer" option. Use IP restriction for max safety! DO NOT SHARE YOUR API KEY and SECRET KEY with ANYONE!! It is your own choice to use this integration!
![2024-11-21 15_28_27-Greenshot image editor](https://github.com/user-attachments/assets/d1fb4449-024e-4342-b4e6-c8827f530182)
2. In Home Assistant, go to **Settings** > **Integrations** > **Add Integration**.
3. Search for and select **Binance**.
4. Enter your API key and API secret.
5. Select the coin pairs you want to track.
6. Click **Submit**.

## Sensors

This integration creates the following sensors:

* **Binance Futures {symbol} Price:** A price sensor for each selected futures pair. Please see attributes of sensor
* **Binance Spot {symbol} Price:** A price sensor for each selected spot pair. Please see attributes of sensor
* **Binance Wallet Balance:** A sensor that shows the total balance in your Binance wallet. Please see attributes of sensor

## Supported Coin Pairs

This integration supports all coin pairs that are supported by Binance Spot and COIN Futures Markets.

## Troubleshooting

If you encounter any issues, please ask for help on the Home Assistant community forums or Discord server.

## Contributing

If you want to contribute to this integration, please visit the GitHub repository.

## License

This integration is licensed under the MIT License.
