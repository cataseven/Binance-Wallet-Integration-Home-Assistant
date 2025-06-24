# Home Assistant Binance Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)

This integration for Home Assistant allows you to track YOUR WALLET BALANCES from your Binance account and prices of spot and futures pairs real-time.

<!-- ![Integration Screenshot](https://.../screenshot.png) -->
<!-- Add a screenshot of the integration in the UI here -->

## Features

* Displays balances of all your wallets (Spot, Funding, Futures etc.) as separate sensors in both BTC and USDT.
* Real-time price tracking for selected Spot and Futures pairs.
* Detailed data for each pair, including price, daily change percentage, and 24-hour high/low.
* Easy installation and updates via HACS.
* Full configuration support through the Home Assistant UI.

## Installation

**Prerequisite:** You must have [HACS](https://hacs.xyz/) installed on your Home Assistant system before installing this integration.

1.  Navigate to HACS > Integrations.
2.  Click the "Explore & Download Repositories" button in the bottom right corner.
3.  Search for "Binance" and click on the result.
4.  Click the "Download" button to install.
5.  Restart Home Assistant.

## Configuration

1.  Navigate to **Settings** > **Devices & Services**.
2.  Click the **"Add Integration"** button in the bottom right.
3.  Search for and select **"Binance"** from the list.
4. Create an API key and API secret for your Binance account on Binance website. https://www.binance.com/en/my/settings/api-management
   - IMPORTANT!!! Please only use Enable Reading (Read Only) API.
   - If you do not want to create sensor for your Wallet Balance do not check "Permits Universal Transfer" option.
   - If you want to use Wallet Balances sensor check "Permits Universal Transfer" option.
   - If you have static IP Use IP restriction for max safety!
   - DO NOT SHARE YOUR API KEY and SECRET KEY with ANYONE!!
   - It is your own responsibility to manage your api credentials!
     
![image1](images/Api1.png)

6. Input your API Key and Secret Key to configuration page.
7.  Select the Spot and Futures pairs you want to track.
8.  Click "Submit".

The integration is now set up! Your sensors will be available.

## Support

If you encounter any issues or have a suggestion for improvement, please open an issue on the [GitHub Issues](https://github.com/cataseven/Home-Assistant-Binance-Integration/issues) page.
