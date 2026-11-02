package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	defaultHNURL         = "https://hn.algolia.com/api/v1"
	defaultBlueskyURL    = "https://api.bsky.app"
	defaultRedditPubURL  = "https://www.reddit.com"
	defaultRedditAPIURL  = "https://oauth.reddit.com"
	defaultRedditAuthURL = "https://www.reddit.com"
	defaultStackURL      = "https://api.stackexchange.com/2.3"
	perPlatformLimit     = 5
	snippetLimit         = 400
)

// SupportedPlatforms lists the platform identifiers accepted by search_social_reactions.
var SupportedPlatforms = []string{"hackernews", "bluesky", "reddit", "stackexchange"}

// SocialItem is one normalized post/thread from any platform.
type SocialItem struct {
	Platform string `json:"platform"`
	Title    string `json:"title,omitempty"`
	Snippet  string `json:"snippet,omitempty"`
	URL      string `json:"url"`
	Author   string `json:"author,omitempty"`
	Score    int    `json:"score"`
	Comments int    `json:"comments"`
}

// SocialResult is the merged fan-out response; failed platforms are reported in Errors.
type SocialResult struct {
	Results []SocialItem      `json:"results"`
	Errors  map[string]string `json:"errors,omitempty"`
}

// SocialSearcher fans out a query to several open social APIs concurrently.
type SocialSearcher struct {
	HTTP               *http.Client
	HNBaseURL          string
	BlueskyURL         string
	RedditURL          string // base for search; oauth host when credentials are set
	RedditAuthURL      string
	StackURL           string
	UserAgent          string
	RedditClientID     string
	RedditClientSecret string

	tokenMu     sync.Mutex
	redditToken string
	tokenExpiry time.Time
}

// NewSocialSearcher builds a searcher pointed at the real public endpoints.
func NewSocialSearcher(redditID, redditSecret, userAgent string) *SocialSearcher {
	s := &SocialSearcher{
		HTTP:               &http.Client{Timeout: 15 * time.Second},
		HNBaseURL:          defaultHNURL,
		BlueskyURL:         defaultBlueskyURL,
		RedditURL:          defaultRedditPubURL,
		RedditAuthURL:      defaultRedditAuthURL,
		StackURL:           defaultStackURL,
		UserAgent:          userAgent,
		RedditClientID:     redditID,
		RedditClientSecret: redditSecret,
	}
	if s.UserAgent == "" {
		s.UserAgent = "AgenticYouTubeAnalyzer/1.0"
	}
	if redditID != "" && redditSecret != "" {
		s.RedditURL = defaultRedditAPIURL
	}
	return s
}

// Search queries each requested platform in its own goroutine and merges the results.
func (s *SocialSearcher) Search(ctx context.Context, query string, platforms []string) SocialResult {
	type outcome struct {
		platform string
		items    []SocialItem
		err      error
	}

	seen := map[string]bool{}
	var wanted []string
	for _, p := range platforms {
		p = strings.ToLower(strings.TrimSpace(p))
		if p != "" && !seen[p] {
			seen[p] = true
			wanted = append(wanted, p)
		}
	}

	ch := make(chan outcome, len(wanted))
	var wg sync.WaitGroup
	for _, p := range wanted {
		wg.Add(1)
		go func(p string) {
			defer wg.Done()
			var items []SocialItem
			var err error
			switch p {
			case "hackernews":
				items, err = s.searchHN(ctx, query)
			case "bluesky":
				items, err = s.searchBluesky(ctx, query)
			case "reddit":
				items, err = s.searchReddit(ctx, query)
			case "stackexchange":
				items, err = s.searchStack(ctx, query)
			default:
				err = fmt.Errorf("unsupported platform (supported: %s)", strings.Join(SupportedPlatforms, ", "))
			}
			ch <- outcome{p, items, err}
		}(p)
	}
	wg.Wait()
	close(ch)

	byPlatform := map[string][]SocialItem{}
	res := SocialResult{Results: []SocialItem{}, Errors: map[string]string{}}
	for o := range ch {
		if o.err != nil {
			res.Errors[o.platform] = o.err.Error()
			continue
		}
		byPlatform[o.platform] = o.items
	}
	for _, p := range wanted { // deterministic ordering
		res.Results = append(res.Results, byPlatform[p]...)
	}
	if len(res.Errors) == 0 {
		res.Errors = nil
	}
	return res
}

func (s *SocialSearcher) getJSON(ctx context.Context, rawURL string, headers map[string]string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", s.UserAgent)
	req.Header.Set("Accept", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	client := s.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("status %d: %s", resp.StatusCode, truncate(strings.TrimSpace(string(body)), 200))
	}
	return json.Unmarshal(body, out)
}

func (s *SocialSearcher) searchHN(ctx context.Context, query string) ([]SocialItem, error) {
	var r struct {
		Hits []struct {
			Title       string `json:"title"`
			URL         string `json:"url"`
			Author      string `json:"author"`
			Points      int    `json:"points"`
			NumComments int    `json:"num_comments"`
			ObjectID    string `json:"objectID"`
		} `json:"hits"`
	}
	q := url.Values{"query": {query}, "tags": {"story"}, "hitsPerPage": {fmt.Sprint(perPlatformLimit)}}
	if err := s.getJSON(ctx, s.HNBaseURL+"/search?"+q.Encode(), nil, &r); err != nil {
		return nil, err
	}
	items := []SocialItem{}
	for _, h := range r.Hits {
		link := h.URL
		if link == "" {
			link = "https://news.ycombinator.com/item?id=" + h.ObjectID
		}
		items = append(items, SocialItem{Platform: "hackernews", Title: h.Title, URL: link, Author: h.Author, Score: h.Points, Comments: h.NumComments})
	}
	return items, nil
}

func (s *SocialSearcher) searchBluesky(ctx context.Context, query string) ([]SocialItem, error) {
	var r struct {
		Posts []struct {
			URI    string `json:"uri"`
			Author struct {
				Handle string `json:"handle"`
			} `json:"author"`
			Record struct {
				Text string `json:"text"`
			} `json:"record"`
			LikeCount  int `json:"likeCount"`
			ReplyCount int `json:"replyCount"`
		} `json:"posts"`
	}
	q := url.Values{"q": {query}, "limit": {fmt.Sprint(perPlatformLimit)}, "sort": {"top"}}
	if err := s.getJSON(ctx, s.BlueskyURL+"/xrpc/app.bsky.feed.searchPosts?"+q.Encode(), nil, &r); err != nil {
		return nil, err
	}
	items := []SocialItem{}
	for _, p := range r.Posts {
		rkey := p.URI[strings.LastIndex(p.URI, "/")+1:]
		items = append(items, SocialItem{
			Platform: "bluesky",
			Snippet:  truncate(p.Record.Text, snippetLimit),
			URL:      fmt.Sprintf("https://bsky.app/profile/%s/post/%s", p.Author.Handle, rkey),
			Author:   p.Author.Handle,
			Score:    p.LikeCount,
			Comments: p.ReplyCount,
		})
	}
	return items, nil
}

func (s *SocialSearcher) searchReddit(ctx context.Context, query string) ([]SocialItem, error) {
	base, path, headers := s.RedditURL, "/search.json", map[string]string(nil)
	if s.RedditClientID != "" && s.RedditClientSecret != "" {
		if tok, err := s.redditAccessToken(ctx); err == nil {
			path, headers = "/search", map[string]string{"Authorization": "bearer " + tok}
		} else {
			log.Printf("[WARN] reddit oauth failed, falling back to public search: %v", err)
			base = defaultRedditPubURL
		}
	}
	var r struct {
		Data struct {
			Children []struct {
				Data struct {
					Title       string `json:"title"`
					Permalink   string `json:"permalink"`
					Subreddit   string `json:"subreddit"`
					Author      string `json:"author"`
					Selftext    string `json:"selftext"`
					Score       int    `json:"score"`
					NumComments int    `json:"num_comments"`
				} `json:"data"`
			} `json:"children"`
		} `json:"data"`
	}
	q := url.Values{"q": {query}, "sort": {"relevance"}, "limit": {fmt.Sprint(perPlatformLimit)}, "raw_json": {"1"}}
	if err := s.getJSON(ctx, base+path+"?"+q.Encode(), headers, &r); err != nil {
		return nil, err
	}
	items := []SocialItem{}
	for _, c := range r.Data.Children {
		d := c.Data
		items = append(items, SocialItem{
			Platform: "reddit",
			Title:    d.Title,
			Snippet:  truncate(d.Selftext, snippetLimit),
			URL:      "https://www.reddit.com" + d.Permalink,
			Author:   d.Author,
			Score:    d.Score,
			Comments: d.NumComments,
		})
	}
	return items, nil
}

func (s *SocialSearcher) redditAccessToken(ctx context.Context) (string, error) {
	s.tokenMu.Lock()
	defer s.tokenMu.Unlock()
	if s.redditToken != "" && time.Now().Before(s.tokenExpiry) {
		return s.redditToken, nil
	}

	form := strings.NewReader("grant_type=client_credentials")
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.RedditAuthURL+"/api/v1/access_token", form)
	if err != nil {
		return "", err
	}
	req.SetBasicAuth(s.RedditClientID, s.RedditClientSecret)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", s.UserAgent)

	client := s.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("reddit token request failed: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("reddit token status %d: %s", resp.StatusCode, truncate(string(body), 200))
	}
	var t struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.Unmarshal(body, &t); err != nil || t.AccessToken == "" {
		return "", errors.New("reddit token response missing access_token")
	}
	s.redditToken = t.AccessToken
	s.tokenExpiry = time.Now().Add(time.Duration(t.ExpiresIn-60) * time.Second)
	return s.redditToken, nil
}

func (s *SocialSearcher) searchStack(ctx context.Context, query string) ([]SocialItem, error) {
	var r struct {
		Items []struct {
			Title       string `json:"title"`
			Link        string `json:"link"`
			Score       int    `json:"score"`
			AnswerCount int    `json:"answer_count"`
			Owner       struct {
				DisplayName string `json:"display_name"`
			} `json:"owner"`
		} `json:"items"`
	}
	q := url.Values{"q": {query}, "site": {"stackoverflow"}, "order": {"desc"}, "sort": {"relevance"}, "pagesize": {fmt.Sprint(perPlatformLimit)}}
	if err := s.getJSON(ctx, s.StackURL+"/search/advanced?"+q.Encode(), nil, &r); err != nil {
		return nil, err
	}
	items := []SocialItem{}
	for _, it := range r.Items {
		items = append(items, SocialItem{Platform: "stackexchange", Title: it.Title, URL: it.Link, Author: it.Owner.DisplayName, Score: it.Score, Comments: it.AnswerCount})
	}
	return items, nil
}
