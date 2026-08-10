"""Fixtures for extraction lab testing.

Contains the 7 required test messages from .agent/plans/menhir-extraction-prompt-recency-recall-research.md
plus the 3 real RCA cases (830ce83f, 852ce960, 2698e78f).
"""

from datetime import datetime

from menhir.explorer.extraction_lab import (
    EpisodeFixture,
    ExtractionLabRequest,
    GoldExtraction,
    ExtractionLabArm,
    ExtractionLabTuning,
)

# Test fixture: Direct named update (from research plan)
FIXTURE_DIRECT_NAMED_UPDATE = ExtractionLabRequest(
    current_message="Rachel actually just moved back to the suburbs again.",
    previous_episodes=[],
    gold=GoldExtraction(
        mentions=["Rachel", "suburbs"],
        propositions=["Rachel moved to or currently resides in the suburbs"],
        update_language=["actually", "moved back", "again"],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Test fixture: Pronoun update (from research plan)
FIXTURE_PRONOUN_UPDATE = ExtractionLabRequest(
    current_message="She actually moved back to the suburbs again.",
    previous_episodes=[],
    gold=GoldExtraction(
        mentions=[],  # Without context, should extract as unresolved female reference
        propositions=[],
        update_language=["actually", "moved back", "again"],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Test fixture: Informal location (from research plan)
FIXTURE_INFORMAL_LOCATION = ExtractionLabRequest(
    current_message="David is living downtown now.",
    previous_episodes=[],
    gold=GoldExtraction(
        mentions=["David", "downtown"],
        propositions=["David currently lives downtown"],
        update_language=["now"],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Test fixture: Corrected monetary value (from research plan)
FIXTURE_CORRECTED_MONETARY_VALUE = ExtractionLabRequest(
    current_message="Actually, I spent $400,000, not $350,000.",
    previous_episodes=[],
    gold=GoldExtraction(
        mentions=["user", "$400,000", "$350,000"],
        propositions=[
            "current expenditure amount is $400,000",
            "rejected prior value $350,000",
        ],
        update_language=["Actually", "not"],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Test fixture: Reversal (from research plan)
FIXTURE_REVERSAL = ExtractionLabRequest(
    current_message="Maya no longer works at Google; she joined Microsoft.",
    previous_episodes=[],
    gold=GoldExtraction(
        mentions=["Maya", "Google", "Microsoft"],
        propositions=[
            "Maya no longer works at Google",
            "Maya works at Microsoft",
        ],
        update_language=["no longer", "joined"],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Test fixture: Generic non-fact (from research plan)
FIXTURE_GENERIC_NON_FACT = ExtractionLabRequest(
    current_message="Moving is really stressful and confusing.",
    previous_episodes=[],
    gold=GoldExtraction(
        mentions=[],  # Should not extract generic statements as durable facts
        propositions=[],
        update_language=[],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Test fixture: Unsupported implication (from research plan)
FIXTURE_UNSUPPORTED_IMPLICATION = ExtractionLabRequest(
    current_message="Rachel has been packing boxes all week.",
    previous_episodes=[],
    gold=GoldExtraction(
        mentions=["Rachel"],
        propositions=[],  # Should NOT invent "Rachel moved"
        update_language=[],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Real RCA case 830ce83f: Rachel's residence (Chicago → suburbs)
FIXTURE_RCA_830CE83F = ExtractionLabRequest(
    source_namespace="lme-830ce83f",
    current_message=(
        "user: Miami Beach sounds fun, but I've been there before. I'm thinking of "
        "somewhere more relaxed. My friend Rachel actually just moved back to the "
        "suburbs again, so I was thinking of somewhere not too far from a major city. "
        "Any suggestions?"
    ),
    previous_episodes=[
        EpisodeFixture(
            text="user: I'm planning a trip with friends and we're deciding between a beach town in Florida and a hiking trip in Colorado. Can you give me some information about the best times to visit these places?",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: What a great dilemma! Both Florida beach towns and Colorado hiking spots are amazing destinations. Timing is everything, so I'd be happy to help you decide when to visit each place.\n\n**Florida Beach Towns:**\n\nThe best time to visit Florida beach towns largely depends on your preferences regarding weather, crowds, and activities. Here are some factors to consider:\n\n* **Weather:** Florida has a subtropical climate, with warm temperatures year-round. The best time to visit is from December to April, when the weather is mild (average highs in the mid-70s to mid-80s Fahrenheit) and humidity is relatively low.\n* **Hurricane season:** The official hurricane season in Florida is from June 1 to November 30. While the chances of a hurricane hitting during your trip are relatively low, it's essential to be aware of the risks.\n* **Crowds and prices:** Peak season in Florida beach towns is from December to April, with the highest demand and prices for accommodations, especially during winter holidays. If you're looking for smaller crowds and lower prices, consider visiting during the shoulder season (May to June or September to November).\n* **Events and festivals:** Many Florida beach towns host festivals and events throughout the year, such as the Key West Lobsterfest (August) or the Fort Lauderdale International Boat Show (October). Research the specific town you're interested in to see what's happening during your planned visit.\n\nSome popular Florida beach towns include:\n\n* Miami Beach\n* Fort Lauderdale\n* Key West\n* Destin\n* Clearwater Beach\n* St. Pete Beach\n\n**Colorado Hiking Spots:**\n\nColorado is a year-round hiking destination, but the best time to visit depends on your preferences regarding weather, trail conditions, and outdoor activities. Here are some factors to consider:\n\n* **Weather:** Colorado's mountains have a high-altitude climate, with cold winters and mild summers. The best time to visit for hiking is from June to September, when the weather is warm (average highs in the 60s to 70s Fahrenheit) and trail conditions are generally dry and accessible.\n* **Snow and winter activities:** If you're interested in winter sports like skiing, snowshoeing, or ice climbing, visit Colorado from December to March. Many hiking trails are snow-covered during this time, but some lower-elevation trails may remain accessible.\n* **Crowds and prices:** Peak season for hiking in Colorado is during the summer months, with the highest demand for accommodations and trail usage. If you prefer fewer crowds and lower prices, consider visiting during the shoulder season (April to May or September to October).\n* **Wildflowers and fall foliage:** If you want to see Colorado's famous wildflowers or enjoy the fall foliage, plan your visit for July to August or September to October, respectively.\n\nSome popular Colorado hiking spots include:\n\n* Rocky Mountain National Park\n* Garden of the Gods (Colorado Springs)\n* Indian Peaks Wilderness\n* Maroon Bells (Aspen)\n* Great Sand Dunes National Park\n\nNow, consider your preferences and priorities:\n\n* If you prefer warmer weather and beach activities, Florida might be the better choice, especially during the winter months.\n* If you prefer cooler weather and hiking, Colorado is an excellent option, especially during the summer months.\n* If you're looking for a mix of outdoor activities and festivals, consider visiting Florida during the shoulder season or Colorado during the spring or fall.\n\nI hope this helps you make an informed decision about your trip with friends!",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I'm also thinking about visiting my friend Rachel who recently moved to a new apartment in the city. Do you know what the weather is like in the city this time of year?",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: I'm happy to help! However, I need a bit more information from you. You didn't mention which city Rachel lives in. Could you please tell me the city and state (or country) where Rachel resides? That way, I can provide you with more accurate information about the weather conditions in that area during your planned visit.\n\nOnce I know the city, I can give you an idea of what to expect in terms of temperature, precipitation, and any other relevant weather-related factors that might impact your visit.",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: What are some good neighborhoods to stay in when visiting Rachel in Chicago, considering we'll want to explore the city and meet up with her easily?",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: Chicago! The Windy City!\n\nChicago's weather can be quite unpredictable, but I'll give you an idea of what to expect during different times of the year.\n\n**Spring (March to May)**:\n\n* Temperatures gradually warm up, with average highs ranging from 45°F (7°C) in March to 64°F (18°C) in May.\n* Spring showers are common, with April being the wettest month.\n* Expect some windy days, but it's a great time to enjoy the city's parks and outdoor attractions as the weather improves.\n\n**Summer (June to August)**:\n\n* Summers in Chicago are warm and humid, with average highs often reaching the mid-80s to low 90s (29°C to 32°C).\n* July is usually the hottest month, with occasional heatwaves.\n* It's a great time to enjoy the city's beaches, outdoor festivals, and events, but be prepared for potential thunderstorms.\n\n**Fall (September to November)**:\n\n* Autumn in Chicago is mild, with average highs ranging from 64°F (18°C) in September to 48°F (9°C) in November.\n* The fall foliage is beautiful, with many trees turning golden, orange, and red.\n* Expect some crisp, cool days, but it's a great time to explore the city's outdoor attractions without the summer crowds.\n\n**Winter (December to February)**:\n\n* Winters in Chicago can be cold and snowy, with average lows often below freezing (32°F or 0°C).\n* January is usually the coldest month, with occasional polar vortex events bringing extremely cold temperatures.\n* If you visit during winter, bundle up, and enjoy the city's indoor attractions, such as museums, theaters, and shopping centers.\n\nSince you didn't specify the exact dates of your visit, I'll leave it up to you to plan accordingly based on the season. If you have any more specific questions or concerns about visiting Chicago, feel free to ask!",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I'll ask Rachel which neighborhood she's living in, and then I can narrow down the options for accommodations. Thanks for the suggestions!",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: Chicago has many wonderful neighborhoods to stay in, each with its unique character and advantages. Considering you want to explore the city and meet up with Rachel easily, I'll suggest some neighborhoods that offer a great balance of convenience, amenities, and accessibility:\n\n1. **The Loop** (Downtown Chicago):\n\t* Pros: Central location, close to major attractions, public transportation hubs, and Rachel's apartment (if she's in the Loop).\n\t* Cons: Can be busy and noisy, limited affordable accommodation options.\n2. **River North**:\n\t* Pros: Trendy neighborhood with many restaurants, bars, and shops, close to the Loop and public transportation.\n\t* Cons: Can be pricey, noisy, and crowded.\n3. **Lincoln Park**:\n\t* Pros: Beautiful parks, lakefront access, upscale amenities, and a more laid-back atmosphere.\n\t* Cons: A bit farther from the Loop, but still accessible by public transportation.\n4. **Lakeview**:\n\t* Pros: Vibrant neighborhood with a mix of restaurants, bars, and shops, close to Wrigley Field and Lake Michigan.\n\t* Cons: Can be busy, especially on weekends, and parking can be challenging.\n5. **Logan Square**:\n\t* Pros: Hip neighborhood with a thriving arts scene, trendy restaurants, and a more affordable price point.\n\t* Cons: A bit farther from the Loop, but still accessible by public transportation.\n6. **West Loop**:\n\t* Pros: Up-and-coming neighborhood with a mix of old and new attractions, close to the Loop and public transportation.\n\t* Cons: Can be a bit industrial, but gentrification is bringing new life to the area.\n\nWhen choosing a neighborhood, consider the following factors:\n\n* Distance to Rachel's apartment: If she's in a specific neighborhood, it might be convenient to stay nearby.\n* Public transportation: Look for neighborhoods with easy access to 'L' train stations (elevated trains) or bus routes.\n* Amenities: Think about what you want to do during your visit – if you want to explore restaurants, bars, and shops, River North or Lincoln Park might be a good fit. If you prefer a more laid-back atmosphere, Lakeview or Logan Square could be a better choice.\n* Budget: Prices vary greatly depending on the neighborhood and accommodation type. Be prepared to pay more for convenience and amenities.\n\nOnce you've narrowed down your options, you can start looking for accommodations, such as hotels, Airbnb apartments, or hostels. Make sure to research and book in advance, especially during peak travel seasons.\n\nRemember to ask Rachel for her input and recommendations, as she can provide valuable insights into the city and its neighborhoods!",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: You're welcome! That's a great idea to ask Rachel about her neighborhood, as it'll give you a better sense of the area and help you choose a convenient and comfortable place to stay.\n\nRemember to also ask her about her favorite spots in the city, restaurants, and activities she recommends, and any tips she might have for navigating Chicago. As a local, she'll be able to provide valuable insider knowledge to make your trip even more enjoyable.\n\nIf you have any more questions or need further assistance, feel free to ask. Otherwise, I hope you have a fantastic trip to Chicago and a great time catching up with Rachel!",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I'll ask Rachel about her neighborhood and favorite spots in the city. And, by the way, do you have any recommendations for good coffee shops or cafes in Chicago?",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: Chicago is a coffee lover's paradise, with a thriving coffee culture and plenty of excellent cafes to choose from. Here are some popular and highly-recommended coffee shops and cafes in Chicago:\n\n1. **Intelligentsia Coffee**: A Chicago staple with multiple locations throughout the city. They source high-quality beans and offer expertly brewed coffee.\n2. **La Colombe Coffee Roasters**: Another popular coffee roaster with multiple locations. They're known for their expertly roasted beans and beautifully designed cafes.\n3. **The Allis**: Located in the West Loop, The Allis is a stylish cafe serving Intelligentsia coffee, along with delicious pastries and sandwiches.\n4. **Sawada Coffee**: A small, independent coffee shop in the West Loop with a loyal following. They source high-quality beans and offer a cozy atmosphere.\n5. **Dark Matter Coffee**: A popular spot in Wicker Park with a quirky, eclectic vibe. They source unique and exotic beans from around the world.\n6. **The Coffee Studio**: A coffee lab and cafe in the Lakeview neighborhood, offering expertly brewed coffee and a rotating selection of single-origin beans.\n7. **Gaslight Coffee Roasters**: A Logan Square staple with a cozy atmosphere and a focus on sustainable, small-batch coffee roasting.\n\nThese are just a few of the many excellent coffee shops in Chicago. You can also ask Rachel for her favorite spots, as she might have some insider knowledge on the best local cafes.\n\nRemember to also try some of Chicago's famous food spots, like deep-dish pizza, Italian beef sandwiches, and classic hot dogs. Enjoy your coffee and culinary adventures in Chicago!",
            created_at=datetime.fromisoformat('2023-05-21T09:14:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I'm thinking of planning a trip with some friends, and I was wondering if you could suggest some good beach towns in Florida that are easy to get to?",
            created_at=datetime.fromisoformat('2023-05-26T03:55:00.000000+00:00'),
        ),
    ],
    gold=GoldExtraction(
        mentions=["Rachel", "suburbs"],
        propositions=["Rachel moved to or currently resides in the suburbs"],
        update_language=["actually", "moved back", "again"],
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Real RCA case 852ce960: mortgage pre-approval ($350k → $400k)
FIXTURE_RCA_852CE960 = ExtractionLabRequest(
    source_namespace="lme-852ce960",
    current_message=(
        "user: I'm planning to move into my new home soon and I need to set up cable "
        "and TV services. Can you recommend some providers in my area and their prices? "
        "By the way, I'm really looking forward to finally owning a home, it's been a "
        "long process, but it'll be worth it to have a backyard like the one I'll have "
        "- remember when I got pre-approved for $400,000 from Wells Fargo?"
    ),
    previous_episodes=[
        EpisodeFixture(
            text="user: I'm in the process of buying a new home and I'm trying to finalize my budget. Can you help me estimate how much I'll need for closing costs?",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: I'd be happy to help you estimate your closing costs.\n\nClosing costs are fees associated with the home buying process that are typically paid at the closing of the transaction. These costs can vary depending on several factors, such as the location, type of property, and type of loan you're using. Here are some common closing costs and estimated ranges to help you plan:\n\n1. **Title Insurance and Escrow Fees**:\n\t* Title insurance: 0.5% to 1.0% of the purchase price\n\t* Escrow fees: $500 to $2,000\n2. **Loan Origination Fees**:\n\t* Origination fee: 0.5% to 1.0% of the loan amount\n\t* Underwriting fee: $300 to $900\n\t* Appraisal fee: $300 to $1,000\n3. **Appraisal Fee**:\n\t* $300 to $1,000 (already included in loan origination fees above)\n4. **Credit Report Fee**:\n\t* $15 to $30\n5. **Flood Determination Fee**:\n\t* $15 to $50\n6. **Inspection Fees** (optional):\n\t* Home inspection: $300 to $1,000\n\t* Pest inspection: $50 to $150\n\t* Septic inspection: $100 to $300\n7. **Attorney Fees** (optional):\n\t* $500 to $2,000\n8. **Recording Fees**:\n\t* $50 to $200\n9. **Transfer Taxes** (varies by state and locality):\n\t* 0.1% to 2.0% of the purchase price\n10. **Other Fees**:\n\t* Courier fees: $20 to $100\n\t* Notary fees: $20 to $100\n\nTo estimate your total closing costs, you can use the following ranges:\n\n* For a conventional loan: 2% to 5% of the purchase price\n* For an FHA loan: 2.5% to 6% of the purchase price\n* For a VA loan: 1.5% to 3% of the purchase price\n* For a USDA loan: 1% to 3% of the purchase price\n\nBased on these estimates, if you're purchasing a $250,000 home, your closing costs could range from:\n\n* $5,000 to $12,500 (conventional loan)\n* $6,250 to $15,000 (FHA loan)\n* $3,750 to $7,500 (VA loan)\n* $2,500 to $7,500 (USDA loan)\n\nKeep in mind that these are rough estimates, and your actual closing costs may vary depending on your specific situation. It's a good idea to review your loan estimate and closing disclosure documents carefully to get a more accurate estimate of your closing costs.\n\nI hope this helps you plan your budget for your new home purchase!",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I'm actually buying a $325,000 house, and I got pre-approved for $350,000 from Wells Fargo. What would be the estimated closing costs for my situation?",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: Can you help me understand how much of the closing costs I can negotiate with the seller to cover, based on the inspection report?',
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: The inspection report can be a powerful tool in negotiating with the seller to cover some or all of the closing costs.\n\nThe extent to which you can negotiate with the seller to cover closing costs depends on several factors, including:\n\n1. **Inspection report findings**: The severity and cost of the repairs or issues identified in the report will impact the seller's willingness to negotiate.\n2. **Local market conditions**: In a buyer's market, you may have more leverage to negotiate. In a seller's market, the seller may be less willing to compromise.\n3. **Original offer and negotiations**: If you've already negotiated the price down or have a strong offer, the seller may be more open to covering some closing costs.\n4. **Seller's motivation**: If the seller is motivated to sell quickly, they may be more willing to cover closing costs to finalize the deal.\n\nHere are some possible scenarios based on the inspection report:\n\n**Minor issues or no major concerns**:\n\n* You may be able to negotiate 0.5% to 1% of the purchase price (around $1,625 to $3,250) to cover some or all of the closing costs.\n\n**Moderate issues or needed repairs**:\n\n* You may be able to negotiate 1% to 2% of the purchase price (around $3,250 to $6,500) to cover some or all of the closing costs, depending on the cost of the repairs.\n\n**Major issues or significant repairs**:\n\n* You may be able to negotiate 2% to 3% of the purchase price (around $6,500 to $9,750) or more to cover some or all of the closing costs, depending on the severity of the issues.\n\n**Requesting credits vs. repairs**:\n\n* Instead of asking the seller to fix the issues, you can request a credit at closing to cover the cost of the repairs. This can be a more appealing option for the seller, as it avoids the hassle of coordinating repairs.\n\nWhen negotiating with the seller, be sure to:\n\n1. **Highlight the inspection report findings**: Emphasize the specific issues and estimated repair costs to demonstrate the impact on the property's value.\n2. **Provide evidence**: Share quotes or estimates from contractors to support your claims.\n3. **Be reasonable**: Don't overreach; be prepared to compromise and find a mutually acceptable solution.\n4. **Put it in writing**: Ensure any agreements or credits are included in the contract or an addendum to avoid misunderstandings.\n\nRemember, negotiation is a give-and-take process. Be prepared to negotiate and find a middle ground that works for both parties. Good luck!",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: I think I can try to negotiate with the seller to cover some of the closing costs, given the minor issues with the roof and plumbing found in the inspection report. Since we already agreed on a credit of $2,000 towards the closing costs, I was thinking of asking the seller to cover an additional 1% of the purchase price, which would be $3,250. Would that be a reasonable request, considering the inspection report findings and the local market conditions?',
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: You\'ve already got a good starting point with the $2,000 credit towards closing costs.\n\nRequesting an additional 1% of the purchase price ($3,250) to cover closing costs is a reasonable ask, considering the minor issues found in the inspection report. Here\'s why:\n\n1. **Inspection report findings**: The minor issues with the roof and plumbing do warrant some consideration. While they\'re not major concerns, they do require attention and will likely need to be addressed in the near future.\n2. **Initial credit already agreed upon**: The seller has already agreed to provide a $2,000 credit towards closing costs, which shows they\'re willing to work with you. Building on that, asking for an additional 1% of the purchase price is a reasonable escalation.\n3. **Local market conditions**: If you\'re in a buyer\'s market or a relatively balanced market, you may have more leverage to negotiate. Even in a seller\'s market, the seller may be willing to compromise to finalize the deal.\n\nTo make your request more effective, consider the following:\n\n1. **Emphasize the cumulative effect**: Highlight the total amount of credits you\'re requesting ($2,000 + $3,250 = $5,250) and how it will benefit both parties by facilitating a smoother closing process.\n2. **Be flexible**: Be open to negotiating the amount or finding alternative solutions, such as the seller paying for specific repairs or providing a home warranty.\n3. **Show your commitment**: Reiterate your enthusiasm for the property and your commitment to moving forward with the purchase, provided the seller is willing to work with you on the closing costs.\n\nHere\'s an example script to help you make your request:\n\n"Hi [Seller\'s Agent], I hope you\'re doing well. I wanted to follow up on our previous discussion regarding the inspection report. As you know, we did identify some minor issues with the roof and plumbing. While they\'re not major concerns, we do believe they warrant some consideration. Given our initial agreement on the $2,000 credit towards closing costs, I was wondering if the seller would be willing to consider an additional 1% of the purchase price ($3,250) to help cover the remaining closing costs. This would bring the total credit to $5,250 and would greatly help us in finalizing the deal. We\'re still very interested in the property and believe this is a reasonable request considering the inspection report findings. Let me know if this is something the seller would be open to discussing."\n\nRemember to stay calm, professional, and respectful during the negotiation process. Good luck!',
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: If the seller agrees to cover the additional $3,250, you'll want to estimate how much you'll need to pay out of pocket for closing costs.\n\nLet's review the estimated closing costs we discussed earlier:\n\n* Total Estimated Closing Costs: $11,332 to $20,322\n* Seller's initial credit: $2,000\n* Additional credit requested: $3,250\n\nIf the seller agrees to the additional credit, the total credit towards closing costs would be:\n\n$2,000 (initial credit) + $3,250 (additional credit) = $5,250\n\nNow, let's subtract the total credit from the estimated closing costs:\n\nLow-end estimate: $11,332 - $5,250 = $6,082\nHigh-end estimate: $20,322 - $5,250 = $15,072\n\nSo, if the seller agrees to cover the additional $3,250, you can expect to pay between $6,082 and $15,072 out of pocket for closing costs.\n\nKeep in mind that this is a rough estimate, and your actual closing costs may vary. Be sure to review your loan estimate and closing disclosure documents carefully to get a more accurate picture of your closing costs.\n\nRemember to also factor in any other expenses associated with buying a home, such as:\n\n* Down payment\n* Prepaids (insurance, taxes, interest)\n* Appraisal fee (if not included in closing costs)\n* Inspection fees (if not included in closing costs)\n* Any other expenses specific to your situation\n\nI hope this helps you plan your finances for the closing process!",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I'll ask the seller if they're willing to cover the additional 1% of the purchase price towards closing costs, given the minor issues found in the inspection report. Can you help me estimate how much I'll need to pay out of pocket for closing costs if the seller agrees to cover the additional $3,250?",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: I'd be happy to help you estimate your monthly mortgage payment, property taxes, and insurance.\n\nBased on your pre-approval for a $350,000 loan, let's assume you'll be financing $325,000 (purchase price) minus $25,000 (down payment) = $300,000.\n\nHere are some estimates:\n\n**Mortgage Payment:**\n\n* Loan amount: $300,000\n* Interest rate: 4% (assuming a 30-year fixed-rate mortgage)\n* Monthly payment: approximately $1,432\n\n**Property Taxes:**\n\n* Property tax rate: 1.25% of the purchase price (this may vary depending on your location)\n* Annual property taxes: $4,062.50 (1.25% of $325,000)\n* Monthly property taxes: approximately $338.54\n\n**Insurance:**\n\n* Homeowners insurance rate: 0.8% of the purchase price (this may vary depending on your location and other factors)\n* Annual insurance premium: $2,600 (0.8% of $325,000)\n* Monthly insurance premium: approximately $216.67\n\n**Total Monthly Payment:**\n\n* Mortgage payment: $1,432\n* Property taxes: $338.54\n* Insurance: $216.67\n* Total: approximately $1,987.21\n\nKeep in mind that these are rough estimates and may vary depending on your individual circumstances. Your actual mortgage payment, property taxes, and insurance premiums may be higher or lower.\n\nIt's essential to review your loan estimate and closing disclosure documents carefully to get a more accurate picture of your monthly payments. Additionally, you may want to consider other costs associated with homeownership, such as maintenance, repairs, and utilities.\n\nI hope this helps you plan your finances for your new home!",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: Can you help me understand how much I'll need to pay each month for my mortgage payment, property taxes, and insurance?",
            created_at=datetime.fromisoformat('2023-08-11T05:59:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I'm in the 78749 zip code. Also, I need to set up electricity and gas services as well. Can you give me a list of providers for those as well?",
            created_at=datetime.fromisoformat('2023-11-30T12:13:00.000000+00:00'),
        ),
    ],
    gold=GoldExtraction(
        mentions=["Wells Fargo", "$400,000"],
        propositions=["pre-approved for $400,000 from Wells Fargo"],
        update_language=["remember", "when"],  # Casual restatement, not explicit correction
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Real RCA case 2698e78f: therapy session frequency (every two weeks → every week)
FIXTURE_RCA_2698E78F = ExtractionLabRequest(
    source_namespace="lme-2698e78f",
    current_message=(
        "user: I see what you're doing here, but I think it's a bit too structured for "
        "me. I'd rather have some flexibility in my schedule. Can you help me come up "
        "with a more general plan to prioritize my tasks and set boundaries, rather "
        "than a strict schedule? And by the way, speaking of boundaries, I see Dr. "
        "Smith every week, and she's been helping me work on this stuff."
    ),
    # NOTE: this case is unique among the three -- neither old nor new frequency
    # survived in the graph initially (see the RCA), suggesting a more severe
    # extraction fidelity issue than 830ce83f/852ce960's "old value kept" pattern.
    previous_episodes=[
        EpisodeFixture(
            text='assistant: Learning to say "no" and set healthy boundaries is an essential skill for maintaining your mental and emotional well-being! It\'s great that Dr. Smith is guiding you in this direction.\n\nSaying "no" and setting boundaries can be challenging, especially if you\'re someone who tends to prioritize others\' needs over your own. However, with practice and the right mindset, you can learn to do so without feeling guilty or anxious. Here are some tips to get you started:\n\n1. **Recognize your limits:** Understand that you can\'t do everything and be everything to everyone. Acknowledge your limitations and prioritize your own needs.\n2. **Identify your values:** Reflect on what\'s truly important to you and allocate your time and energy accordingly. This will help you make decisions that align with your values and goals.\n3. **Practice saying "no" in low-stakes situations:** Start small by saying "no" to minor requests, like declining an invitation to a movie or turning down a second helping of food. This will help you build confidence and develop a sense of assertiveness.\n4. **Use the 3Rs:** When asked to commit to something, respond with the 3Rs:\n\t* **Reflect:** Take a moment to consider the request and your priorities.\n\t* **Respond:** Say "I need some time to think about it" or "Let me check my schedule."\n\t* **Renegotiate:** If you decide to accept, negotiate the terms to ensure they work for you.\n5. **Offer alternatives:** Instead of saying "no" outright, suggest alternative solutions that work better for you. For example, "I\'m not available to meet on Fridays, but I can meet on Wednesdays."\n6. **Be firm but polite:** When saying "no," be clear, direct, and respectful. Avoid apologetic or hesitant language, which can be seen as an invitation to negotiate. Use phrases like "I appreciate the invite, but I need to prioritize my own commitments right now."\n7. **Remember that "no" is a complete sentence:** You don\'t owe anyone an explanation for your decision. A simple "no, thank you" or "I\'m not able to commit to that" is enough.\n8. **Prepare for pushback:** Some people might resist or guilt-trip you into changing your mind. Anticipate this and stay firm in your decision. You can say, "I understand you\'re disappointed, but I\'ve made up my mind."\n9. **Celebrate your "no"s:** Acknowledge and celebrate each time you successfully set a boundary or say "no" to a commitment that doesn\'t serve you. This will help you build confidence and reinforce the importance of prioritizing your own needs.\n10. **Seek support:** Share your struggles with Dr. Smith, friends, or family members you trust. They can offer guidance, encouragement, and support as you develop your boundary-setting skills.\n\nRemember, setting healthy boundaries and saying "no" is not about being selfish; it\'s about taking care of yourself and maintaining your emotional and mental well-being.\n\nHow do you feel about trying some of these strategies? Is there a specific situation where you\'d like to practice saying "no" or setting a boundary?',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I've been having trouble falling asleep some nights despite trying journaling and a consistent sleep schedule. Can you suggest some additional strategies to help me improve my sleep quality?",
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I've been trying to prioritize self-care activities like yoga and meditation, and it's been helping with my anxiety levels. Do you think these activities can also help with sleep quality? And by the way, I have a therapy session with Dr. Smith coming up soon - it's every two weeks, so I'm looking forward to discussing my progress with her.",
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: I\'m happy to help! Beyond journaling and a consistent sleep schedule, here are some additional strategies to help improve your sleep quality:\n\n1. **Create a sleep-conducive environment**:\n\t* Make your bedroom a sleep sanctuary: dark, quiet, and cool (around 60-67°F).\n\t* Invest in blackout curtains, earplugs, or a white noise machine if necessary.\n\t* Consider using a weighted blanket or a comfortable mattress.\n2. **Establish a relaxing bedtime routine**:\n\t* Develop a calming pre-sleep routine, such as:\n\t\t+ Reading a book (not on a screen)\n\t\t+ Listening to soothing music or nature sounds\n\t\t+ Practicing gentle stretches or yoga\n\t\t+ Enjoying a warm bath or shower\n\t* Aim to start winding down at least 30 minutes before bedtime\n3. **Limit exposure to screens and stimulating activities before bed**:\n\t* Avoid screens (phones, tablets, laptops, TVs) for at least an hour before bedtime, as the blue light can suppress melatonin production.\n\t* Try to avoid stimulating activities like exercise, playing video games, or watching exciting movies at least 2-3 hours before bedtime.\n4. **Manage stress and anxiety**:\n\t* Practice relaxation techniques, such as deep breathing, progressive muscle relaxation, or meditation, to calm your mind and body.\n\t* Consider keeping a "worry journal" to write down concerns before bed, so you can clear your mind.\n5. **Get regular exercise, but not too close to bedtime**:\n\t* Regular physical activity can help improve sleep quality, but avoid vigorous exercise within 3-4 hours of bedtime, as it can actually interfere with sleep.\n6. **Limit caffeine, nicotine, and alcohol**:\n\t* Avoid consuming these substances in the afternoon and evening, as they can disrupt sleep patterns and reduce sleep quality.\n7. **Try aromatherapy and essential oils**:\n\t* Certain scents like lavender, chamomile, and valerian root can promote relaxation and improve sleep. Use a diffuser or apply a few drops to your pillowcase.\n8. **Get some morning sunlight**:\n\t* Exposure to natural light in the morning helps regulate your circadian rhythms, which can improve sleep quality.\n9. **Consider supplements and herbal remedies**:\n\t* Certain supplements like melatonin, magnesium, and GABA may help with sleep. Consult with a healthcare professional before adding any new supplements to your routine.\n\t* Herbal teas like chamomile, valerian root, or passionflower can also promote relaxation and improve sleep.\n10. **Seek professional help if needed**:\n\t* If you continue to struggle with sleep, consider consulting a healthcare professional or a sleep specialist who can help you identify underlying issues and develop a personalized plan.\n\nRemember, it may take some experimentation to find the right combination of strategies that work for you. Be patient, and don\'t hesitate to reach out if you need further guidance.',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: You\'re welcome! I\'m proud of you for taking this step towards setting healthy boundaries in your relationship. Remember, it\'s a conversation, not a confrontation. Approach it with empathy, understanding, and an open mind.\n\n**Before you go:**\n\n1. **Take a few deep breaths:** Calm your nerves and center yourself before the conversation.\n2. **Visualize a positive outcome:** Imagine the conversation going well and your partner understanding your perspective.\n3. **Remember your why:** Reflect on why you\'re setting these boundaries and how they\'ll positively impact your relationship and personal well-being.\n\n**During the conversation:**\n\n1. **Speak from the heart:** Be genuine and authentic in your communication.\n2. **Use "I" statements:** Express your feelings and needs using "I" statements to avoid blame or accusation.\n3. **Listen actively:** Pay attention to your partner\'s concerns and respond thoughtfully.\n\n**After the conversation:**\n\n1. **Be patient:** Give your partner time to process and adjust to the new boundaries.\n2. **Follow up:** Check in with your partner to ensure the boundaries are working for both of you and make adjustments as needed.\n3. **Celebrate your success:** Acknowledge and celebrate your courage in setting boundaries and taking care of yourself.\n\nYou got this! Go out there and have a productive, respectful conversation with your partner. Remember, setting boundaries is a sign of strength, not weakness.',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: Excellent decision to have this conversation! Approaching it calmly and using "I" statements will definitely help. Now, let\'s prepare you for potential pushback or resistance from your partner:\n\n**Anticipate their concerns:**\n\n1. **Understand their perspective:** Put yourself in your partner\'s shoes and consider why they might be resistant to your boundaries. Are they worried about spending less time together? Do they feel like you\'re changing the dynamics of your relationship?\n2. **Acknowledge their feelings:** Show empathy and understanding when they express their concerns. This will help them feel heard and validated.\n\n**Responding to pushback:**\n\n1. **Stay calm and assertive:** Maintain your calm tone and assertively reiterate your boundaries. Avoid getting defensive or aggressive, as this can escalate the situation.\n2. **Reiterate your reasons:** Clearly explain your reasons for setting boundaries, using "I" statements to emphasize your feelings and needs. Avoid blaming or accusing language.\n3. **Offer alternatives:** Suggest alternative solutions that still meet your partner\'s needs while respecting your boundaries. For example, "I understand you want to spend time together, but I need some alone time. How about we plan a fun activity for next weekend, and I\'ll make sure to set aside dedicated time for us?"\n4. **Use active listening:** Pay attention to your partner\'s concerns and respond thoughtfully. This will help them feel heard and understood.\n5. **Reaffirm your commitment:** Let your partner know that setting boundaries is not about pulling away from the relationship, but about maintaining a healthy and balanced dynamic.\n\n**Common pushback scenarios:**\n\n1. **"You\'re changing the rules":** "I understand it might feel that way, but I\'m just trying to prioritize my own needs and find a better balance in our relationship."\n2. **"You\'re not fun anymore":** "I\'m still the same person, but I need some time to recharge. I promise I\'ll be more energized and present when we do spend time together."\n3. **"You\'re abandoning me":** "I\'m not abandoning you, but I need some space to focus on myself. This will actually help me be a better partner to you in the long run."\n\n**Remember:**\n\n1. **You\'re not asking for permission:** You\'re setting boundaries to take care of yourself, and that\'s okay. You don\'t need your partner\'s permission to prioritize your own needs.\n2. **You\'re not being selfish:** Setting boundaries is not selfish; it\'s essential for maintaining a healthy and balanced relationship.\n3. **You\'re doing this for both of you:** By setting boundaries, you\'re ultimately creating a more sustainable and fulfilling relationship for both partners.\n\nYou\'ve got this! Go into the conversation with confidence, empathy, and an open mind. Remember to breathe, stay calm, and prioritize your own needs.',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I've been thinking about setting boundaries with my partner, especially when it comes to social commitments. They tend to invite me to a lot of events, but sometimes I just want to stay home and relax. Do you think it's okay to set boundaries with your partner, and if so, how do I bring it up with them?",
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: I think I\'m ready to have this conversation with my partner. I\'ll try to approach it calmly and use "I" statements to express my feelings. Do you have any tips on how to handle potential pushback or resistance from my partner?',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: I think I\'m ready to have this conversation with my partner. I\'ll try to stay calm and use "I" statements to express my feelings. I\'ll also anticipate their concerns and respond thoughtfully. Thanks for the tips!',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: I\'ve been feeling overwhelmed with commitments lately, and Dr. Smith suggested I learn to say "no" and set healthy boundaries. Do you have any tips on how to do this without feeling guilty or anxious?',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: That's great to hear that you've been prioritizing self-care activities like yoga and meditation! These practices can indeed have a positive impact on sleep quality.\n\n**Yoga and sleep:**\n\n1. **Reduces stress and anxiety:** Yoga can help calm the mind and body, making it easier to fall asleep and stay asleep.\n2. **Regulates cortisol levels:** Yoga can help regulate cortisol levels, which can become imbalanced and disrupt sleep patterns.\n3. **Improves sleep quality:** Studies have shown that regular yoga practice can improve sleep quality, duration, and depth.\n4. **Increases melatonin:** Some yoga practices, such as restorative yoga or yin yoga, can help increase melatonin levels, promoting a restful night's sleep.\n\n**Meditation and sleep:**\n\n1. **Reduces stress and anxiety:** Meditation can calm the mind and body, making it easier to fall asleep.\n2. **Increases melatonin:** Regular meditation practice can increase melatonin levels, promoting a restful night's sleep.\n3. **Improves sleep quality:** Meditation can help improve sleep quality, duration, and depth.\n4. **Enhances sleep stages:** Meditation can help increase the amount of deep sleep and REM sleep, leading to better overall sleep quality.\n\nIt's wonderful that you're committed to your therapy sessions with Dr. Smith! Discussing your progress with her will likely help you gain further insights and develop strategies to improve your sleep quality.\n\n**Tips for your therapy session:**\n\n1. **Share your sleep concerns:** Be open with Dr. Smith about your sleep struggles and how they're affecting your daily life.\n2. **Discuss your self-care routine:** Share your experiences with yoga and meditation, and how they've been helping with your anxiety levels.\n3. **Explore underlying issues:** Work with Dr. Smith to identify any underlying issues that might be contributing to your sleep difficulties.\n4. **Develop a sleep-focused plan:** Collaborate with Dr. Smith to create a plan tailored to your specific sleep needs and goals.\n\nRemember to be patient and kind to yourself throughout your journey. It may take some time to find the right combination of strategies that work for you, but with persistence and the support of Dr. Smith, you can improve your sleep quality and overall well-being.",
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: Setting boundaries with your partner is essential for maintaining a healthy and balanced relationship. It\'s completely okay to establish boundaries, especially when it comes to social commitments, to ensure you\'re taking care of yourself and respecting each other\'s needs.\n\n**Why setting boundaries with your partner is important:**\n\n1. **Maintains individuality:** Boundaries help you preserve your individuality and autonomy within the relationship.\n2. **Prevents resentment:** Setting boundaries can prevent feelings of resentment and frustration that may arise from constantly accommodating your partner\'s requests.\n3. **Encourages communication:** Establishing boundaries promotes open and honest communication, which is crucial for a healthy relationship.\n4. **Respects each other\'s needs:** Boundaries ensure that both partners respect each other\'s needs, desires, and limitations.\n\n**How to bring it up with your partner:**\n\n1. **Choose the right time:** Find a comfortable and private setting where you both feel relaxed and can have an open conversation.\n2. **Use "I" statements:** Start with "I" statements to express your feelings and needs, rather than accusing or blaming your partner. For example, "I feel overwhelmed when I\'m invited to multiple events in a week. I need some downtime to recharge."\n3. **Be specific:** Clearly communicate your boundaries and expectations. In this case, you might say, "I\'d like to limit our social commitments to one or two events per week, so I can have some time to myself."\n4. **Listen to their perspective:** Give your partner space to share their thoughts and feelings. They might have valid reasons for inviting you to events or have concerns about your boundaries.\n5. **Find a compromise:** Work together to find a compromise that respects both of your needs. You might suggest alternative solutions, like taking turns choosing events or having regular "staycation" nights.\n6. **Be open to feedback and adjustments:** Be willing to revisit and adjust your boundaries as needed. Relationships are dynamic, and your needs may change over time.\n\n**Example script to get you started:**\n\n"Hey, love. I wanted to talk to you about our social calendar. I\'ve been feeling overwhelmed lately with all the events we\'ve been attending. I need some downtime to recharge and focus on myself. Would it be okay if we limited our social commitments to one or two events per week? I still want to spend time with you, but I need some space to relax and unwind. What do you think?"\n\nRemember to approach the conversation with empathy and understanding. Your partner might not realize the impact their invitations have on you, and working together, you can find a solution that strengthens your relationship.',
            created_at=datetime.fromisoformat('2023-04-03T02:03:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I've been having trouble falling asleep some nights despite trying journaling and a consistent sleep schedule. Can you suggest some additional strategies to help me improve my sleep quality?",
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: I\'m happy to help! Beyond journaling and a consistent sleep schedule, here are some additional strategies to help improve your sleep quality:\n\n1. **Create a sleep-conducive environment**:\n\t* Make your bedroom a sleep sanctuary: dark, quiet, and cool (around 60-67°F).\n\t* Invest in blackout curtains, earplugs, or a white noise machine if necessary.\n\t* Consider using a weighted blanket or a comfortable mattress.\n2. **Establish a relaxing bedtime routine**:\n\t* Develop a calming pre-sleep routine, such as:\n\t\t+ Reading a book (not on a screen)\n\t\t+ Listening to soothing music or nature sounds\n\t\t+ Practicing gentle stretches or yoga\n\t\t+ Enjoying a warm bath or shower\n\t* Aim to start winding down at least 30 minutes before bedtime\n3. **Limit exposure to screens and stimulating activities before bed**:\n\t* Avoid screens (phones, tablets, laptops, TVs) for at least an hour before bedtime, as the blue light can suppress melatonin production.\n\t* Try to avoid stimulating activities like exercise, playing video games, or watching exciting movies at least 2-3 hours before bedtime.\n4. **Manage stress and anxiety**:\n\t* Practice relaxation techniques, such as deep breathing, progressive muscle relaxation, or meditation, to calm your mind and body.\n\t* Consider keeping a "worry journal" to write down concerns before bed, so you can clear your mind.\n5. **Get regular exercise, but not too close to bedtime**:\n\t* Regular physical activity can help improve sleep quality, but avoid vigorous exercise within 3-4 hours of bedtime, as it can actually interfere with sleep.\n6. **Limit caffeine, nicotine, and alcohol**:\n\t* Avoid consuming these substances in the afternoon and evening, as they can disrupt sleep patterns and reduce sleep quality.\n7. **Try aromatherapy and essential oils**:\n\t* Certain scents like lavender, chamomile, and valerian root can promote relaxation and improve sleep. Use a diffuser or apply a few drops to your pillowcase.\n8. **Get some morning sunlight**:\n\t* Exposure to natural light in the morning helps regulate your circadian rhythms, which can improve sleep quality.\n9. **Consider supplements and herbal remedies**:\n\t* Certain supplements like melatonin, magnesium, and GABA may help with sleep. Consult with a healthcare professional before adding any new supplements to your routine.\n\t* Herbal teas like chamomile, valerian root, or passionflower can also promote relaxation and improve sleep.\n10. **Seek professional help if needed**:\n\t* If you continue to struggle with sleep, consider consulting a healthcare professional or a sleep specialist who can help you identify underlying issues and develop a personalized plan.\n\nRemember, it may take some experimentation to find the right combination of strategies that work for you. Be patient, and don\'t hesitate to reach out if you need further guidance.',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: I think I\'m ready to have this conversation with my partner. I\'ll try to approach it calmly and use "I" statements to express my feelings. Do you have any tips on how to handle potential pushback or resistance from my partner?',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: I\'ve been feeling overwhelmed with commitments lately, and Dr. Smith suggested I learn to say "no" and set healthy boundaries. Do you have any tips on how to do this without feeling guilty or anxious?',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: Learning to say "no" and set healthy boundaries is an essential skill for maintaining your mental and emotional well-being! It\'s great that Dr. Smith is guiding you in this direction.\n\nSaying "no" and setting boundaries can be challenging, especially if you\'re someone who tends to prioritize others\' needs over your own. However, with practice and the right mindset, you can learn to do so without feeling guilty or anxious. Here are some tips to get you started:\n\n1. **Recognize your limits:** Understand that you can\'t do everything and be everything to everyone. Acknowledge your limitations and prioritize your own needs.\n2. **Identify your values:** Reflect on what\'s truly important to you and allocate your time and energy accordingly. This will help you make decisions that align with your values and goals.\n3. **Practice saying "no" in low-stakes situations:** Start small by saying "no" to minor requests, like declining an invitation to a movie or turning down a second helping of food. This will help you build confidence and develop a sense of assertiveness.\n4. **Use the 3Rs:** When asked to commit to something, respond with the 3Rs:\n\t* **Reflect:** Take a moment to consider the request and your priorities.\n\t* **Respond:** Say "I need some time to think about it" or "Let me check my schedule."\n\t* **Renegotiate:** If you decide to accept, negotiate the terms to ensure they work for you.\n5. **Offer alternatives:** Instead of saying "no" outright, suggest alternative solutions that work better for you. For example, "I\'m not available to meet on Fridays, but I can meet on Wednesdays."\n6. **Be firm but polite:** When saying "no," be clear, direct, and respectful. Avoid apologetic or hesitant language, which can be seen as an invitation to negotiate. Use phrases like "I appreciate the invite, but I need to prioritize my own commitments right now."\n7. **Remember that "no" is a complete sentence:** You don\'t owe anyone an explanation for your decision. A simple "no, thank you" or "I\'m not able to commit to that" is enough.\n8. **Prepare for pushback:** Some people might resist or guilt-trip you into changing your mind. Anticipate this and stay firm in your decision. You can say, "I understand you\'re disappointed, but I\'ve made up my mind."\n9. **Celebrate your "no"s:** Acknowledge and celebrate each time you successfully set a boundary or say "no" to a commitment that doesn\'t serve you. This will help you build confidence and reinforce the importance of prioritizing your own needs.\n10. **Seek support:** Share your struggles with Dr. Smith, friends, or family members you trust. They can offer guidance, encouragement, and support as you develop your boundary-setting skills.\n\nRemember, setting healthy boundaries and saying "no" is not about being selfish; it\'s about taking care of yourself and maintaining your emotional and mental well-being.\n\nHow do you feel about trying some of these strategies? Is there a specific situation where you\'d like to practice saying "no" or setting a boundary?',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: You\'re welcome! I\'m proud of you for taking this step towards setting healthy boundaries in your relationship. Remember, it\'s a conversation, not a confrontation. Approach it with empathy, understanding, and an open mind.\n\n**Before you go:**\n\n1. **Take a few deep breaths:** Calm your nerves and center yourself before the conversation.\n2. **Visualize a positive outcome:** Imagine the conversation going well and your partner understanding your perspective.\n3. **Remember your why:** Reflect on why you\'re setting these boundaries and how they\'ll positively impact your relationship and personal well-being.\n\n**During the conversation:**\n\n1. **Speak from the heart:** Be genuine and authentic in your communication.\n2. **Use "I" statements:** Express your feelings and needs using "I" statements to avoid blame or accusation.\n3. **Listen actively:** Pay attention to your partner\'s concerns and respond thoughtfully.\n\n**After the conversation:**\n\n1. **Be patient:** Give your partner time to process and adjust to the new boundaries.\n2. **Follow up:** Check in with your partner to ensure the boundaries are working for both of you and make adjustments as needed.\n3. **Celebrate your success:** Acknowledge and celebrate your courage in setting boundaries and taking care of yourself.\n\nYou got this! Go out there and have a productive, respectful conversation with your partner. Remember, setting boundaries is a sign of strength, not weakness.',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: Excellent decision to have this conversation! Approaching it calmly and using "I" statements will definitely help. Now, let\'s prepare you for potential pushback or resistance from your partner:\n\n**Anticipate their concerns:**\n\n1. **Understand their perspective:** Put yourself in your partner\'s shoes and consider why they might be resistant to your boundaries. Are they worried about spending less time together? Do they feel like you\'re changing the dynamics of your relationship?\n2. **Acknowledge their feelings:** Show empathy and understanding when they express their concerns. This will help them feel heard and validated.\n\n**Responding to pushback:**\n\n1. **Stay calm and assertive:** Maintain your calm tone and assertively reiterate your boundaries. Avoid getting defensive or aggressive, as this can escalate the situation.\n2. **Reiterate your reasons:** Clearly explain your reasons for setting boundaries, using "I" statements to emphasize your feelings and needs. Avoid blaming or accusing language.\n3. **Offer alternatives:** Suggest alternative solutions that still meet your partner\'s needs while respecting your boundaries. For example, "I understand you want to spend time together, but I need some alone time. How about we plan a fun activity for next weekend, and I\'ll make sure to set aside dedicated time for us?"\n4. **Use active listening:** Pay attention to your partner\'s concerns and respond thoughtfully. This will help them feel heard and understood.\n5. **Reaffirm your commitment:** Let your partner know that setting boundaries is not about pulling away from the relationship, but about maintaining a healthy and balanced dynamic.\n\n**Common pushback scenarios:**\n\n1. **"You\'re changing the rules":** "I understand it might feel that way, but I\'m just trying to prioritize my own needs and find a better balance in our relationship."\n2. **"You\'re not fun anymore":** "I\'m still the same person, but I need some time to recharge. I promise I\'ll be more energized and present when we do spend time together."\n3. **"You\'re abandoning me":** "I\'m not abandoning you, but I need some space to focus on myself. This will actually help me be a better partner to you in the long run."\n\n**Remember:**\n\n1. **You\'re not asking for permission:** You\'re setting boundaries to take care of yourself, and that\'s okay. You don\'t need your partner\'s permission to prioritize your own needs.\n2. **You\'re not being selfish:** Setting boundaries is not selfish; it\'s essential for maintaining a healthy and balanced relationship.\n3. **You\'re doing this for both of you:** By setting boundaries, you\'re ultimately creating a more sustainable and fulfilling relationship for both partners.\n\nYou\'ve got this! Go into the conversation with confidence, empathy, and an open mind. Remember to breathe, stay calm, and prioritize your own needs.',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I've been thinking about setting boundaries with my partner, especially when it comes to social commitments. They tend to invite me to a lot of events, but sometimes I just want to stay home and relax. Do you think it's okay to set boundaries with your partner, and if so, how do I bring it up with them?",
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='user: I think I\'m ready to have this conversation with my partner. I\'ll try to stay calm and use "I" statements to express my feelings. I\'ll also anticipate their concerns and respond thoughtfully. Thanks for the tips!',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="user: I've been trying to prioritize self-care activities like yoga and meditation, and it's been helping with my anxiety levels. Do you think these activities can also help with sleep quality? And by the way, I have a therapy session with Dr. Smith coming up soon - it's every two weeks, so I'm looking forward to discussing my progress with her.",
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text="assistant: That's great to hear that you've been prioritizing self-care activities like yoga and meditation! These practices can indeed have a positive impact on sleep quality.\n\n**Yoga and sleep:**\n\n1. **Reduces stress and anxiety:** Yoga can help calm the mind and body, making it easier to fall asleep and stay asleep.\n2. **Regulates cortisol levels:** Yoga can help regulate cortisol levels, which can become imbalanced and disrupt sleep patterns.\n3. **Improves sleep quality:** Studies have shown that regular yoga practice can improve sleep quality, duration, and depth.\n4. **Increases melatonin:** Some yoga practices, such as restorative yoga or yin yoga, can help increase melatonin levels, promoting a restful night's sleep.\n\n**Meditation and sleep:**\n\n1. **Reduces stress and anxiety:** Meditation can calm the mind and body, making it easier to fall asleep.\n2. **Increases melatonin:** Regular meditation practice can increase melatonin levels, promoting a restful night's sleep.\n3. **Improves sleep quality:** Meditation can help improve sleep quality, duration, and depth.\n4. **Enhances sleep stages:** Meditation can help increase the amount of deep sleep and REM sleep, leading to better overall sleep quality.\n\nIt's wonderful that you're committed to your therapy sessions with Dr. Smith! Discussing your progress with her will likely help you gain further insights and develop strategies to improve your sleep quality.\n\n**Tips for your therapy session:**\n\n1. **Share your sleep concerns:** Be open with Dr. Smith about your sleep struggles and how they're affecting your daily life.\n2. **Discuss your self-care routine:** Share your experiences with yoga and meditation, and how they've been helping with your anxiety levels.\n3. **Explore underlying issues:** Work with Dr. Smith to identify any underlying issues that might be contributing to your sleep difficulties.\n4. **Develop a sleep-focused plan:** Collaborate with Dr. Smith to create a plan tailored to your specific sleep needs and goals.\n\nRemember to be patient and kind to yourself throughout your journey. It may take some time to find the right combination of strategies that work for you, but with persistence and the support of Dr. Smith, you can improve your sleep quality and overall well-being.",
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
        EpisodeFixture(
            text='assistant: Setting boundaries with your partner is essential for maintaining a healthy and balanced relationship. It\'s completely okay to establish boundaries, especially when it comes to social commitments, to ensure you\'re taking care of yourself and respecting each other\'s needs.\n\n**Why setting boundaries with your partner is important:**\n\n1. **Maintains individuality:** Boundaries help you preserve your individuality and autonomy within the relationship.\n2. **Prevents resentment:** Setting boundaries can prevent feelings of resentment and frustration that may arise from constantly accommodating your partner\'s requests.\n3. **Encourages communication:** Establishing boundaries promotes open and honest communication, which is crucial for a healthy relationship.\n4. **Respects each other\'s needs:** Boundaries ensure that both partners respect each other\'s needs, desires, and limitations.\n\n**How to bring it up with your partner:**\n\n1. **Choose the right time:** Find a comfortable and private setting where you both feel relaxed and can have an open conversation.\n2. **Use "I" statements:** Start with "I" statements to express your feelings and needs, rather than accusing or blaming your partner. For example, "I feel overwhelmed when I\'m invited to multiple events in a week. I need some downtime to recharge."\n3. **Be specific:** Clearly communicate your boundaries and expectations. In this case, you might say, "I\'d like to limit our social commitments to one or two events per week, so I can have some time to myself."\n4. **Listen to their perspective:** Give your partner space to share their thoughts and feelings. They might have valid reasons for inviting you to events or have concerns about your boundaries.\n5. **Find a compromise:** Work together to find a compromise that respects both of your needs. You might suggest alternative solutions, like taking turns choosing events or having regular "staycation" nights.\n6. **Be open to feedback and adjustments:** Be willing to revisit and adjust your boundaries as needed. Relationships are dynamic, and your needs may change over time.\n\n**Example script to get you started:**\n\n"Hey, love. I wanted to talk to you about our social calendar. I\'ve been feeling overwhelmed lately with all the events we\'ve been attending. I need some downtime to recharge and focus on myself. Would it be okay if we limited our social commitments to one or two events per week? I still want to spend time with you, but I need some space to relax and unwind. What do you think?"\n\nRemember to approach the conversation with empathy and understanding. Your partner might not realize the impact their invitations have on you, and working together, you can find a solution that strengthens your relationship.',
            created_at=datetime.fromisoformat('2023-05-20T19:23:00.000000+00:00'),
        ),
    ],
    gold=GoldExtraction(
        mentions=["Dr. Smith", "every week"],
        propositions=["I see Dr. Smith every week"],
        update_language=[],  # Plain restatement, no explicit update language
    ),
    arms=[
        ExtractionLabArm(
            id="baseline",
            label="Baseline",
            tuning=ExtractionLabTuning(prompt_variant="baseline"),
        ),
    ],
)

# Collection of all fixtures for easy access
TEST_FIXTURES = [
    FIXTURE_DIRECT_NAMED_UPDATE,
    FIXTURE_PRONOUN_UPDATE,
    FIXTURE_INFORMAL_LOCATION,
    FIXTURE_CORRECTED_MONETARY_VALUE,
    FIXTURE_REVERSAL,
    FIXTURE_GENERIC_NON_FACT,
    FIXTURE_UNSUPPORTED_IMPLICATION,
]

RCA_FIXTURES = [
    FIXTURE_RCA_830CE83F,
    FIXTURE_RCA_852CE960,
    FIXTURE_RCA_2698E78F,
]

ALL_FIXTURES = TEST_FIXTURES + RCA_FIXTURES
