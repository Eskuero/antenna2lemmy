# Migrate your "antenna alien" site posts to Lemmy
This python script can iterate over a list of posts links, obtaining all the data necessary and creating a clone in your Lemmy instance of choice.

## Disclaimer
This is provided as-is **without warranty or responsibility** of the usage you make out of it. **I'm not responsible** if you break, crash, overload or do any harmful action over your instance and hardware. I'm not responsible over the content you migrate and you are the one to make sure you have rights or permissions to make copies of it.

## Usage
Clone this repository with git:

    $ git clone https://github.com/Eskuero/antenna2lemmy
    $ cd antenna2lemmy

You need to modify the **provided sample config.hjson**. It's properly documented with comments. It's easier if you install the dependencies in a virtual environment instead of relying on your distribution packages:

    $ virtualenv env
    $ . env/bin/activate
    $ pip install -r requirements.txt

Now you need to add as many URL of posts to a text file, one on each new line. Then you can run the program like this where communityname is the name of the target community and links.txt the file containing the links:

    $ python antenna2lemmy communityname,links.txt

## Todo

 - Obviously Lemmy API doesn't allow to specify a score for a post on creation so restoring a original ranking is not possible. We could modify specify the amount of votes it has on the database after creation and then upvote it once via API but I'm unsure it would be satisfactory for already federating communities.
 - The same happens for creation dates. But this is probably never meant to be updated so modifying it once on the database would mean nothing for federation.
