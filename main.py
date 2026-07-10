from scrape_news import fetch_latest_news
from mcp_handler import MCPHandler

def main():
    news_url = 'https://example-news-website.com'  # Replace with a real news website URL
    api_key = 'your_mcp_api_key'  # Replace with your actual MCP API key
    
    latest_news = fetch_latest_news(news_url)
    
    mcp_handler = MCPHandler(api_key)
    pipeline_status = mcp_handler.create_pipeline(latest_news)
    dashboard_status = mcp_handler.create_dashboard(latest_news)
    
    print(pipeline_status)
    print(dashboard_status)

if __name__ == '__main__':
    main()
