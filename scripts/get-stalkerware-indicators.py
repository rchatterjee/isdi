import pandas as pd
import yaml
import requests
import config

IOC_FILE = "/tmp/ioc.yaml"

# get stalkerware indicators from IOC stalkware indicators repository
def download_ioc():
    url = "https://raw.githubusercontent.com/AssoEchap/stalkerware-indicators/refs/heads/master/ioc.yaml"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            ioc = yaml.safe_load(response.text )
            return pd.DataFrame(ioc)
        else:
            print("Error downloading IOC file: {}".format(response.status_code))
            return pd.DataFrame()
    except requests.exceptions.RequestException as e:
        print("Error downloading IOC file: {}".format(e))
        return pd.DataFrame()
    except yaml.YAMLError as e:
        print("Error parsing YAML IOC file: {}".format(e))
    except Exception as e:
        print("Other error reading IOC file: {}".format(e))


def merge_with_appflags(fname, newd):
    """Merge new stalkerware indicators with existing app flags."""
    newd['title'] = newd.name + '|' + newd.names.str.join(', ').fillna('')
    newd = newd.explode("packages")
    newd['appId'] = newd['packages']
    newd['store'] = 'offstore'
    newd['flag'] = 'spyware'  
    # ['appId', 'store', 'flag', 'title']
    d = pd.read_csv(fname)
    pd.concat([newd[d.columns], d], ignore_index=True)\
        .drop_duplicates(['appId', 'store']).to_csv(fname, index=False)

if __name__ == "__main__":
    # download stalkerware indicators
    newd = download_ioc()
    if newd.empty:
        print("No stalkerware indicators found")
        exit(1)
    # merge with app flags
    merge_with_appflags(config.APP_FLAGS_FILE, newd)
