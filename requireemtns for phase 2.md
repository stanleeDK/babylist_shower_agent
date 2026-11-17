Value prop

1. I am an agent that can help you organize your baby shower.   
2. I can suggest venus near where you are, both restaurants also also park picnic areas.  
3. You can choose a date or  I can email your friends and family and organize a date  
4. I can triangulate that date with availability dates at the venues (picnic tables or restaurants).  
5. If you decide to have the shower at home, i can skip that but also suggest a basket from instacart   
6. I stop short of actually booking restaurants, tables or instacart baskets 

Discovery 

1. In the copy explain   
   1. If you try this agent, you can enter a raffle to quality for a baby box, or discounts of up to 50% on baby shop items.   
2. In a two column design  
   1. Column 1   
      1. Chat like interface where statically the following are requested from the user  
         1. Location   
            1. Do you want to host it at your home? Or at one of these venues (venues are shown on the right column) but the user has to choose at home vs out of home   
         2. Get a date or a range   
            1. You have a specific date in mind?   
            2. OR  
            3. You have a window in the future?   
               1. Tomorrow and 88 days into future  
               2. 89 to 111 days into future  
               3. 112 to 149 days into future   
         3. Have a value prop that explains “give me permission to connect to your gmail account and I can email your invitees and organize their rsvps on your behalf. I can also email restaurants about availability” insert cta to connect to google   
         4. Return user to chat after permission granted   
         5. Have an input box with autocomplete that allows the user to enter in people’s email addresses they wwant to invite   
            1. This is also where the demo mode can be enabled so it prepopulates with the three personas   
         6. Valid output combinations   
            1. Specific Date \+ In home   
               1. Go into the send email mode but only show email invites getting sent, and then simulate rsvps coming back.   
               2. Suggest to the user that the agent can suggest a list of supplies to buy for the in home party and if the user accepts then it will use a prompt to the llm create  list of products (eg finger food, decorations, drinks etc) and then show that shopping list in the second column with a options to do devleiery, or pick up, and a cta to add to a kroger shopping cart (kroge also requires location so use the same inferred location otherwise default to 94118 zip code,   
            2. Specific Date \+ out of home   
               1. Go into the send email mode but only show email invites getting sent,   
               2. and the same time show a carousel of restaurants and picnic tables, let the use select one.   
                  1. If restaurant, send email to restaurant asking for availability on that date, simulate a response coming back saying yes   
                  2. If picnic table, show link to picnic table url to book it   
                     1. \[same as option 6bii above in the Specific Date \+ In home  category\] Suggest to the user that the agent can suggest a list of supplies to buy for the in home party and if the user accepts then it will use a prompt to the llm create  list of products (eg finger food, decorations, drinks etc) and then show that shopping list in the second column with a options to do devleiery, or pick up, and a cta to add to a kroger shopping cart (kroge also requires location so use the same inferred location otherwise default to 94118 zip code,   
                     2.   
            3. Date rate \+ in home   
               1. Go to send email mode but in this case send out emails to invitees and go through the logic of arriving at a proposed date.   
               2. \[same as option 6bii above in the Specific Date \+ In home  category\] Suggest to the user that the agent can suggest a list of supplies to buy for the in home party and if the user accepts then it will use a prompt to the llm create  list of products (eg finger food, decorations, drinks etc) and then show that shopping list in the second column with a options to do devleiery, or pick up, and a cta to add to a kroger shopping cart (kroge also requires location so use the same inferred location otherwise default to 94118 zip code,   
            4. Date range \+ out of home  
               1. Go to send email mode but in this case send out emails to invitees and go through the logic of arriving at a proposed date.   
               2. Once the date is is set, then show the user   
                  1. Range of picnic tables   
                  2. Range of restaurants   
               3. If user chooses picnic table \- take user out to picnic table page to book for that date, bring user back and show the date added   
                  1. \[same as option 6bii above in the Specific Date \+ In home  category\] Suggest to the user that the agent can suggest a list of supplies to buy for the in home party and if the user accepts then it will use a prompt to the llm create  list of products (eg finger food, decorations, drinks etc) and then show that shopping list in the second column with a options to do devleiery, or pick up, and a cta to add to a kroger shopping cart (kroge also requires location so use the same inferred location otherwise default to 94118 zip code,   
               4. If user chooses restaurants, email the restaurant selected asking for availability that matches the invitee rsvp \= yes list  
               5. Simulate the restaurant responding yes.   
   2. Column 2  
      1. From the ip address, infer the location and then present   
         1. 3 restaurants nearby that have large party reservation (10 people) (look at the python/sfparkandrec folder for how an app that shows the methodology for retrieving restaurants and their contact info \+ photos from resy)   
         2. 3 picnic tables nearby that are nearby that support 10 people   
3. General reqs  
   1. I think i want to have a reall llm in the entire flow, right now the first part of harvesting user information like the date and venue type is a structured form that looks like a chat… but i also do want the chat to be limited, so i dont want people going in and asking for the latest sports scores. Lets drill into pros and cons here  
   2. Observability \- i want to keep having a pane where we can see what the agent is doing and thinking, what messages it’s sending and what it is deciding   
   3. Async \- because this is a demo… i need it to be snappy. When looking for restaurants limit to just 3-5. Also when compiling a shopping list keep it to 10 items to keep the wait time short  
   4. Although this is somewhat of a structured flow, build it so that there is an input chat box for the user so the user can  
      1. Change the date \-\> agent wil email users and restaurants to change date. If picnic table set, remind user to cancel it and show them different options to rebook   
      2. Have a button to simulate that a key person can’t make it. Flag that and then basically suggest to the user in the chat about whether they want to reschedule   
4. Implementation notes   
   1. The main code is in python/email\_organizer. I like alot of that esp the actual event planner phase that shows a visual of what dates work \- I want to keep that some how and if the flow where a restaurant is involved add the restaurant to that grid view that visualizes common availability. I like the email threads going back and forth. However the requirements above also require a two pane layout with the chat on the left and the venues/shopping lists on right. So need to figure out where to put the   
      1. Agent train of thought / thinking / decisions thread window   
      2. Grid ov availability   
      3. Users chat bot input  
      4. Buttons to trigger new inputs like emails from vips changing dates and also to reset the demo   
   2. For the shopping list feature, I have created a proto type that uses kroger’s api and it takes a prompt to then generate a list. This is the implementiatn logic that should be mimicked. I can provide the kroger api key but the agent in this app should provide the prompte (essentially doing an inhouse baby shower so the list should reflect that or a picnic table and the shopping list should reflect that. Show the photos of the products   
   3. For the restaurant feature, there is also a proof of concept in the /sfparkandrec project. Basically replicate that but instead of 25 restaurants lets limit to the top 3 and filter on ones that have email addresses (try to make it fast). Show the photos of the restaurants.t   
   4. For the park picnic table feature, use the /actualsfpark project, there i have a web app that loads all nearby picnic tables with their images available for booking. With photos which should be shown in the app 

