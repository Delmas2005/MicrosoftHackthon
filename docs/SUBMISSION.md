# Submission text

## Project name
The Fourth Official

## One line
What the referee could actually see, the Law that applies, and the decision, explained live on the broadcast, the stadium screen and every fan's phone, and never before the officials decide.

## Pitch (about 220 words)
74% of Premier League fans say the reasoning behind VAR decisions is not clear (Football Supporters' Association, 2026). Every camera shows what VAR saw; none shows what the referee could see from where he stood.

The Fourth Official measures it from tracking data. During a VAR check, the TV panel draws the referee's line of sight, the players who hid the incident, and how much of it he could see (0% of the contact, number 5 in the way). Once the officials decide, the same decision reaches three screens: the broadcast, a one-line stadium board, and a "Why?" view on the phone of the supporters who lost the call, in their language, with an audio version read by Azure AI Speech.

Three agents on Microsoft Foundry write the cards: a Laws Clerk that quotes the IFAB Laws word for word from Azure AI Search, a Contradicteur that removes any sentence that says more than its evidence, and a Narrator for each screen. A verifier with no AI blocks any decision announced early and any number that does not match its measurement. If a model fails, a template card stays on screen: the match never waits for AI.

We built it as two students on an Azure for Students account: no credit card, free tiers wherever they exist, and €0.19 of student credit so far. Anyone with a student account can rebuild it without paying.

On RefBench, our benchmark, on 100 incidents they had never seen, the agents reached the right decision more often than the call on the field (90% against 70%) and flagged 16 of 18 clear errors, but a third of their VAR calls were not justified. So the Fourth Official explains decisions; it never makes them.

## Links
- Live demo: https://fourthofficialweb.z28.web.core.windows.net/?screen=wall
- Video: https://youtu.be/dpUy8UCSEo8
- Code: https://github.com/Delmas2005/MicrosoftHackthon

## Azure and Microsoft services used
Microsoft Foundry (gpt-5-mini, gpt-4.1-mini), Azure AI Search (Free), Azure Web PubSub (Free), Azure AI Speech (Free), Application Insights (traces, linked to Foundry), Azure AI Evaluation SDK (groundedness), Azure Storage static website, GitHub Actions. MCP tool server with 12 tools. All on an Azure for Students subscription.

## Data
Fully synthetic: one generated match and 500 benchmark incidents. Every club, player, official and venue is fictional. No real footage or broadcast material.
