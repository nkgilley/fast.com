# fastdotcom

fastdotcom is a Python package that performs internet speed tests using [fast.com](https://fast.com). It measures download and upload speeds, and performs ping tests (both unloaded and loaded) to give you a comprehensive view of your internet performance.

## Features

- **Download Speed Test:** Measures the download speed in Mbps.
- **Upload Speed Test:** Measures the upload speed in Mbps.
- **Ping Tests:** Measures the average unloaded and loaded ping in milliseconds.

The measured values come rather close to the measurements taken on the website. If anyone has an idea of tweaking it even moren, feel free to get in contact.

## Installation

Install fastdotcom from PyPI using pip:

```bash
pip install fastdotcom
```
## Usage

Below is an example of how to use fastdotcom in your Python code:
```bash
import fastdotcom

# Run the speed test with default parameters
result = fastdotcom.fast_com(verbose=False, max_time=10)
print(result)
```

## Example Output

When you run the example above, you might see output similar to:
```bash
{'download_speed': 120.68, 'upload_speed': 25.93, 'unloaded_ping': 19.35, 'loaded_ping': 48.64, 'success': 1}
```

- **Download Speed:** Measured download speed in Mbps (displayed with two decimal places).
- **Upload Speed:** Measured upload speed in Mbps (displayed with two decimal places).
- **Unloaded Ping:** Average ping (latency) when the network is idle.
- **Loaded Ping:** Average ping (latency) during a download test.
- **Success:** An indication if the speed test was performed successfully (zero if no internet connection was available).

## Requirements
- Python 3.8 or newer
- icmplib
- requests

## License

fastdotcom is released under the MIT License.

## Contributing

Contributions are welcome! Please feel free to open an issue or submit a pull request on GitHub.
