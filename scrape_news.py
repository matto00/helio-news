import requests
from bs4 import BeautifulSoup

def fetch_latest_news(url):
    response = requests.get(url)
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'html.parser')
        # Example: Extracting headlines from a news website
        headlines = []
        for headline in soup.find_all('h3', class_='news-headline'):
            headlines.append(headline.text.strip())
        return headlines
    else:
        print(f"Failed to fetch news. Status code: {response.status_code}")
        return []

if __name__ == '__main__':
    url = 'https://example-news-website.com'  # Replace with a real news website URL
    latest_news = fetch_latest_news(url)
    for news in latest_news:
        print(news)
